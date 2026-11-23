"""Planners turn a question into one MCP tool call, or a refusal.

`DeterministicPlanner` is Demo Mode: hand-written rules, no language model involved. A planner only proposes;
the server still validates arguments and enforces identity, scopes and row filters on every call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from ..client.session import ToolInfo

RefusalReason = Literal[
    "unsupported_operation",
    "sensitive_field",
    "insufficient_evidence",
    "out_of_domain",
    "needs_clarification",
    "no_more_results",
    "model_declined",
    "planner_error",
]


class ToolCall(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    identity_claim_ignored: bool = False


class Refusal(BaseModel):
    reason: RefusalReason
    message: str
    identity_claim_ignored: bool = False


Plan = ToolCall | Refusal


@dataclass
class PlanningContext:
    """What a planner may use. `identity` comes from the server's whoami result, never from the question."""

    tools: list[ToolInfo]
    identity: dict[str, Any] | None = None
    last_call: ToolCall | None = None
    next_cursor: str | None = None
    tool_names: set[str] = field(init=False)

    def __post_init__(self) -> None:
        self.tool_names = {tool.name for tool in self.tools}


class Planner(Protocol):
    name: str
    mode: Literal["demo", "llm"]
    description: str

    async def plan(self, question: str, context: PlanningContext) -> Plan: ...


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


SQL = _rx(r"\b(select|insert|update|delete|drop|alter|create|truncate)\b[\s\S]*\b(from|into|table|set|where)\b|;\s*--")
SENSITIVE = _rx(r"bank\s*(account|details)|account\s*number|routing|\biban\b|\bswift\b|password|api\s*key|\bsecret")
WRITE = [
    _rx(r"^\s*(please\s+)?(pay|mark|set|update|change|edit|modify|delete|remove|approve|reject|create|add|cancel|void|"
        r"send|email|forward|transfer|wire|post|record|write|book|reopen|close)\b"),
    _rx(r"\b(mark|flag|set)\b.+\bas\s+(paid|open|disputed|closed|approved)\b"),
]
IDENTITY_CLAIM = [
    _rx(r"\b(i am|i'm|as|act as|acting as|pretend|impersonate|on behalf of|logged in as)\b.{0,40}"
        r"\b(controller|admin|administrator|cora|ava|ben|dev|fay|manager|cfo|root|superuser)\b"),
    _rx(r"\bignore\b.{0,30}\b(instructions|rules|restrictions)\b"),
    _rx(r"admin mode|system override|developer mode"),
]
FORECAST = _rx(r"\b(forecast|predict|projection|project(ed)?|next (week|month|quarter|year)|in the future|will .{0,40}\b(be|look)|"
               r"budget|profit|margin|revenue|ebitda|payroll|salary|credit score)\b")
NEXT_PAGE = _rx(r"\b(next page|more results|show more|see more|load more|continue|the rest|next \d+)\b")
WHOAMI = _rx(r"who am i|\bmy (access|permissions|scopes|role)\b|what (can|am) i (allowed to )?(see|access)")
INVOICE_ID = _rx(r"\b(A[PR])-(\d+)\b")
DETAIL_WORDS = _rx(r"\b(detail|details|memo|tax|supplier|customer|who|everything|full|counterparty|note)\b")
PAYMENT_WORDS = _rx(r"\b(paid|pay|payment|payments|outstanding|owe|owed|balance|left|remaining|status|overdue|late|due)\b")
AR_WORDS = _rx(r"\b(receivables?|ar|customers?|clients?|owed to us|owes? us)\b")
AP_WORDS = _rx(r"\b(payables?|ap|suppliers?|vendors?|bills?|we owe|owe)\b")
SUMMARY = _rx(r"summar|\btotals?\b|breakdown|aggregate|overview|owe the most|\bby (status|supplier|vendor|customer|business unit|unit)\b|"
              r"\bper (supplier|vendor|customer|business unit)\b|\btop \d*\s*(suppliers|customers|vendors)\b")
GROUP_COUNTERPARTY = _rx(r"\b(by|per)\s+(supplier|vendor|customer)s?\b|owe the most|\btop \d*\s*(suppliers|customers|vendors)\b|"
                         r"\bwhich (suppliers|customers|vendors)\b")
GROUP_UNIT = _rx(r"business unit|\b(by|per) (unit|plant|site)\b|\bbu\b")
OVERDUE = _rx(r"overdue|past due|\blate\b")
MORE_THAN_DAYS = _rx(r"\b(more than|over|longer than|greater than)\s+(\d{1,3})\s+days")
AT_LEAST_DAYS = _rx(r"\b(at least|minimum of|>=)\s*(\d{1,3})\s+days|\b(\d{1,3})\+\s*days")
SUPPLIER_SEARCH = [
    _rx(r"\b(search|find|look ?up)\b.*\b(suppliers?|vendors?)\b"),
    _rx(r"\b(suppliers?|vendors?)\s+(named|called|matching|like)\b"),
]
QUOTED = re.compile(r"['\"“”‘’]([^'\"“”‘’]{2,60})['\"“”‘’]")
AFTER_KEYWORD = _rx(r"\b(named|called|matching|like|for)\s+(.+)$")
LIST_WORDS = _rx(r"\b(invoices?|payables|receivables|bills|list|show|which)\b")
COUNTERPARTY_ID = _rx(r"\b(SUP|CUS)-(\d{4})\b")
UNIT_ID = _rx(r"\bBU-([A-Z]{2,10})\b")
UNIT_NAMES = {"east assembly": "BU-EAST", "west machining": "BU-WEST", "south logistics": "BU-SOUTH"}
DUE_BEFORE = _rx(r"due (before|by|until)\s+(\d{4}-\d{2}-\d{2})")
DUE_AFTER = _rx(r"due (after|from|since)\s+(\d{4}-\d{2}-\d{2})")

READ_ONLY_MESSAGE = (
    "This assistant is read-only: it cannot pay, change, approve, cancel or delete records, and it cannot send "
    "data anywhere. Ask a question about invoices, payments or suppliers instead."
)


def _default_ledger(identity: dict[str, Any] | None) -> str | None:
    """When the question names no ledger, use the only ledger the verified caller can read, if there is one."""
    scopes = set((identity or {}).get("scopes") or [])
    ap = bool(scopes & {"ap:read", "ap:read:own"})
    ar = "ar:read" in scopes
    if ap and not ar:
        return "AP"
    if ar and not ap:
        return "AR"
    return None


def _ledger(question: str, identity: dict[str, Any] | None) -> str | None:
    counterparty = COUNTERPARTY_ID.search(question)
    if counterparty:
        return "AP" if counterparty.group(1).upper() == "SUP" else "AR"
    ar = bool(AR_WORDS.search(question))
    ap = bool(AP_WORDS.search(re.sub(r"owe[sd]? us|owed to us", "", question, flags=re.IGNORECASE)))
    if ar != ap:
        return "AR" if ar else "AP"
    return _default_ledger(identity) if not (ar or ap) else None


def _status(question: str) -> str | None:
    low = question.lower()
    if re.search(r"partial(ly)?", low):
        return "PARTIALLY_PAID"
    if re.search(r"disput", low):
        return "DISPUTED"
    if re.search(r"\b(open|unpaid)\b", low):
        return "OPEN"
    if re.search(r"\bpaid\b", low):
        return "PAID"
    return None


def _supplier_query(question: str) -> str | None:
    quoted = QUOTED.search(question)
    if quoted:
        return quoted.group(1).strip()
    after = AFTER_KEYWORD.search(question)
    if after:
        return after.group(2).strip(" ?.!,;:") or None
    return None


class DeterministicPlanner:
    """Demo Mode planner: ordered regular-expression rules. It never calls a language model."""

    name = "DeterministicPlanner"
    mode: Literal["demo", "llm"] = "demo"
    description = "Demo Mode - deterministic rule-based planner, no LLM"

    async def plan(self, question: str, context: PlanningContext) -> Plan:
        q = " ".join(question.split())
        claim = any(pattern.search(q) for pattern in IDENTITY_CLAIM)

        def refuse(reason: RefusalReason, message: str) -> Refusal:
            return Refusal(reason=reason, message=message, identity_claim_ignored=claim)

        def call(tool: str, arguments: dict[str, Any], rationale: str) -> ToolCall:
            return ToolCall(tool=tool, arguments=arguments, rationale=rationale, identity_claim_ignored=claim)

        if not q:
            return refuse("needs_clarification", "Please ask a question about invoices, payments or suppliers.")
        if SQL.search(q):
            return refuse("unsupported_operation",
                          "This assistant cannot run SQL or other free-form queries. It can only call its read-only tools.")
        if SENSITIVE.search(q):
            return refuse("sensitive_field",
                          "Bank account details and credentials are never returned by this server, whatever the caller's role.")
        if any(pattern.search(q) for pattern in WRITE):
            return refuse("unsupported_operation", READ_ONLY_MESSAGE)
        if FORECAST.search(q):
            return refuse("insufficient_evidence",
                          "The dataset only holds recorded invoices and payments up to its as-of date. It has no forecasts, "
                          "budgets or other figures that would support that answer.")
        if NEXT_PAGE.search(q):
            if context.last_call is None:
                return refuse("needs_clarification", "There is no earlier result to continue from. Ask the full question first.")
            if not context.next_cursor:
                return refuse("no_more_results", "The previous result has no further pages.")
            return call(context.last_call.tool, {**context.last_call.arguments, "cursor": context.next_cursor},
                        "Follow-up: next page of the previous result, using its cursor.")
        if WHOAMI.search(q):
            return call("whoami", {}, "Question about the caller's own identity and access.")

        invoice = INVOICE_ID.search(q)
        if invoice:
            invoice_id = f"{invoice.group(1).upper()}-{invoice.group(2)}"
            if not DETAIL_WORDS.search(q) and PAYMENT_WORDS.search(q):
                return call("get_invoice_payment_status", {"invoice_id": invoice_id},
                            "Question names one invoice and asks about payment or due status.")
            return call("get_invoice_details", {"invoice_id": invoice_id}, "Question names one invoice.")

        if any(pattern.search(q) for pattern in SUPPLIER_SEARCH):
            query = _supplier_query(q)
            if not query:
                return refuse("needs_clarification", "Which supplier name should I search for?")
            return call("search_suppliers", {"query": query}, "Supplier directory search by name.")

        ledger = _ledger(q, context.identity)
        if SUMMARY.search(q):
            if ledger is None:
                return refuse("needs_clarification", "Do you mean payables (suppliers) or receivables (customers)?")
            group_by = "counterparty" if GROUP_COUNTERPARTY.search(q) else "business_unit" if GROUP_UNIT.search(q) else "status"
            tool = "summarize_payables" if ledger == "AP" else "summarize_receivables"
            return call(tool, {"group_by": group_by}, f"Aggregate question; grouping by {group_by}.")
        if OVERDUE.search(q):
            if ledger is None:
                return refuse("needs_clarification", "Do you mean overdue payables or overdue receivables?")
            arguments: dict[str, Any] = {"ledger": ledger}
            more, least = MORE_THAN_DAYS.search(q), AT_LEAST_DAYS.search(q)
            if more:
                arguments["min_days_overdue"] = int(more.group(2))
            elif least:
                arguments["min_days_overdue"] = max(int(least.group(2) or least.group(3)) - 1, 0)
            return call("get_overdue_invoices", arguments, "Question about overdue invoices.")
        if LIST_WORDS.search(q) and (ledger or AP_WORDS.search(q) or AR_WORDS.search(q) or re.search(r"invoice", q, re.I)):
            if ledger is None:
                return refuse("needs_clarification", "Do you mean payables (suppliers) or receivables (customers)?")
            arguments = {"ledger": ledger}
            if status := _status(q):
                arguments["status"] = status
            if counterparty := COUNTERPARTY_ID.search(q):
                arguments["counterparty_id"] = f"{counterparty.group(1).upper()}-{counterparty.group(2)}"
            unit = UNIT_ID.search(q)
            if unit:
                arguments["business_unit_id"] = f"BU-{unit.group(1).upper()}"
            else:
                for label, unit_id in UNIT_NAMES.items():
                    if label in q.lower():
                        arguments["business_unit_id"] = unit_id
            if before := DUE_BEFORE.search(q):
                arguments["due_before"] = before.group(2)
            if after := DUE_AFTER.search(q):
                arguments["due_after"] = after.group(2)
            return call("list_invoices", arguments, "Listing question with filters taken from the text.")
        return refuse("out_of_domain",
                      "I can only answer questions about the fictional company's invoices, payments, suppliers and receivables.")
