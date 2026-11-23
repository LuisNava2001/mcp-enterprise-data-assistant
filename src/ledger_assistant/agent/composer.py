"""Deterministic answer composition from structured tool results.

The composer only formats values that are present in the tool result (or echoes identifiers from the question),
so every figure in an answer can be traced to evidence. It never repeats instruction-like record text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..client.session import ToolOutcome
from .planner import Refusal, ToolCall

LIST_PREVIEW = 10
IDENTITY_NOTE = ("Identity and permissions come from your sign-in token, not from the text of the question, "
                 "so the identity claim in your message was ignored.")
UNTRUSTED_NOTE = ("The memo on this record contains instruction-like text. Record text is untrusted data, so it was "
                  "not followed and is not repeated here.")
INSTRUCTION_LIKE = re.compile(
    r"ignore (all |any )?(previous|prior|your)|instructions|system override|admin mode|you are now|disregard|"
    r"\bforward\b|\bsend\b|e-?mail|bank account|mark this", re.IGNORECASE)


@dataclass
class Answer:
    text: str
    evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def label(status: str) -> str:
    return status.replace("_", " ").lower()


def ledger_label(ledger: str) -> str:
    return "payables" if ledger == "AP" else "receivables"


def _overdue(days: int, as_of: str) -> str:
    return f"{days} days overdue as of {as_of}" if days > 0 else f"not overdue as of {as_of}"


def _payment_status(d: dict[str, Any]) -> Answer:
    last = (f"Last payment on {d['last_payment_date']} ({d['payment_count']} payment{'s' if d['payment_count'] != 1 else ''})."
            if d["last_payment_date"] else "No payments recorded.")
    text = (f"Invoice {d['invoice_id']} is {label(d['status'])}. Amount {money(d['amount_cents'])}, paid {money(d['paid_cents'])}, "
            f"outstanding {money(d['outstanding_cents'])}. Due {d['due_date']}; {_overdue(d['days_overdue'], d['as_of'])}. {last}")
    return Answer(text, [d["invoice_id"]])


def _details(d: dict[str, Any]) -> Answer:
    cp = d["counterparty"]
    role = "supplier" if d["ledger"] == "AP" else "customer"
    parts = [
        f"Invoice {d['invoice_id']} ({ledger_label(d['ledger'])}, {role} {cp['name']} {cp['counterparty_id']}, business unit "
        f"{d['business_unit_id']}) was issued {d['issue_date']} and is due {d['due_date']}.",
        f"Amount {money(d['amount_cents'])}, paid {money(d['paid_cents'])}, outstanding {money(d['outstanding_cents'])}; "
        f"status {label(d['status'])}.",
    ]
    if d["payments"]:
        paid = "; ".join(f"{p['paid_on']} {money(p['amount_cents'])} by {p['method']}" for p in d["payments"])
        parts.append(f"Payments: {paid}.")
    else:
        parts.append("No payments recorded.")
    if cp.get("tax_id"):
        how = "masked for your access level" if cp.get("tax_id_masked") else "shown in full because your token includes sensitive:read"
        parts.append(f"Supplier tax ID {cp['tax_id']} ({how}).")
    notes = []
    memo = d.get("memo")
    if memo and INSTRUCTION_LIKE.search(memo):
        notes.append(UNTRUSTED_NOTE)
    elif memo:
        parts.append(f'Memo (untrusted record text): "{memo}"')
    return Answer(" ".join(parts), [d["invoice_id"], cp["counterparty_id"]], notes)


def _page(d: dict[str, Any], tool: str, arguments: dict[str, Any]) -> Answer:
    ledger = arguments.get("ledger", d["items"][0]["ledger"] if d["items"] else "AP")
    kind = "overdue " + ledger_label(ledger) if tool == "get_overdue_invoices" else ledger_label(ledger)
    filters = [f"more than {value} days overdue" if key == "min_days_overdue" else f"{key.replace('_', ' ')} {value}"
               for key, value in arguments.items()
               if key not in {"ledger", "cursor", "limit"} and value not in (None, "", 0)]
    filter_text = f" ({', '.join(filters)})" if filters else ""
    if not d["items"]:
        return Answer(f"No {kind}{filter_text} match. Your access: {d['scope']}. As of {d['as_of']}.")
    order = "most overdue first" if tool == "get_overdue_invoices" else "oldest due date first"
    lines = [f"{d['total_matching']} {kind}{filter_text} match. Your access: {d['scope']}. "
             f"As of {d['as_of']}, this page shows {d['returned']} ({order}):"]
    for item in d["items"][:LIST_PREVIEW]:
        tail = (f"{item['days_overdue']} days overdue" if tool == "get_overdue_invoices"
                else f"due {item['due_date']}, {label(item['status'])}")
        lines.append(f"- {item['invoice_id']} | {item['counterparty_name']} | {tail} | outstanding {money(item['outstanding_cents'])}")
    if d["returned"] > LIST_PREVIEW:
        lines.append(f"- ...and {d['returned'] - LIST_PREVIEW} more on this page.")
    if d["next_cursor"]:
        lines.append("More results are available: ask for the next page.")
    return Answer("\n".join(lines), [item["invoice_id"] for item in d["items"]])


def _summary(d: dict[str, Any]) -> Answer:
    t = d["totals"]
    grouping = {"status": "status", "counterparty": "supplier" if d["ledger"] == "AP" else "customer",
                "business_unit": "business unit"}[d["group_by"]]
    lines = [f"{ledger_label(d['ledger']).capitalize()} by {grouping} within {d['scope']}, as of {d['as_of']}: "
             f"{t['invoice_count']} invoices, amount {money(t['amount_cents'])}, outstanding {money(t['outstanding_cents'])}, "
             f"overdue {money(t['overdue_cents'])}.", "Largest outstanding balances:"]
    for group in d["groups"][:5]:
        lines.append(f"- {group['label']} ({group['key']}): outstanding {money(group['outstanding_cents'])} across "
                     f"{group['invoice_count']} invoices; overdue {money(group['overdue_cents'])}")
    return Answer("\n".join(lines), [group["key"] for group in d["groups"][:5]])


def _suppliers(d: dict[str, Any], arguments: dict[str, Any]) -> Answer:
    query = arguments.get("query", "")
    if not d["items"]:
        return Answer(f'No suppliers within your access ({d["scope"]}) match "{query}".')
    lines = [f'{d["total_matching"]} supplier(s) match "{query}" within {d["scope"]}:']
    for s in d["items"][:LIST_PREVIEW]:
        masked = " (masked)" if s["tax_id_masked"] else ""
        lines.append(f"- {s['name']} ({s['supplier_id']}) | {s['country']} | {s['business_unit_id']} | "
                     f"terms {s['payment_terms_days']} days | tax ID {s['tax_id']}{masked}")
    if d["next_cursor"]:
        lines.append("More results are available: ask for the next page.")
    return Answer("\n".join(lines), [s["supplier_id"] for s in d["items"]])


def _identity(d: dict[str, Any]) -> Answer:
    units = "all business units" if d["business_units"] is None else ", ".join(d["business_units"])
    text = (f"You are signed in as {d['display_name']} ({d['subject']}), role {d['role']}. "
            f"Scopes: {', '.join(d['scopes'])}. Business units: {units}.")
    if d.get("supplier_id"):
        text += f" Supplier portal account for {d['supplier_id']}: you see only that supplier's invoices."
    return Answer(text, [d["subject"]])


def _short_validation(message: str) -> str:
    text = re.sub(r"\s*\[type=.*?\]", "", message or "")
    text = re.sub(r"\d+ validation errors? for \w+Arguments\s*", "", text)
    return text.strip() or "invalid arguments"


def _error(outcome: ToolOutcome) -> Answer:
    code, message = outcome.error_code, outcome.message or ""
    if code == "NOT_FOUND":
        text = f"I could not find that record among the data your account can access. Server response: {message}"
    elif code == "FORBIDDEN":
        text = f"The server denied this request for your account: {message}"
    elif code == "INVALID_ARGUMENT":
        text = f"The server rejected the request arguments: {_short_validation(message)}"
    elif code == "INVALID_CURSOR":
        text = "That page cursor is not valid for this question and account. Ask the question again to start from the first page."
    elif code == "UNAUTHENTICATED":
        text = "The server did not accept your credentials."
    else:
        text = f"The request could not be completed ({code}): {message}"
    return Answer(text)


def compose(plan: ToolCall | Refusal, outcome: ToolOutcome | None) -> Answer:
    if isinstance(plan, Refusal):
        answer = Answer(plan.message)
    elif outcome is None or not outcome.ok or outcome.data is None:
        answer = _error(outcome) if outcome else Answer("No tool result is available.")
    else:
        d, tool = outcome.data, plan.tool
        if tool == "get_invoice_payment_status":
            answer = _payment_status(d)
        elif tool == "get_invoice_details":
            answer = _details(d)
        elif tool in {"list_invoices", "get_overdue_invoices"}:
            answer = _page(d, tool, plan.arguments)
        elif tool in {"summarize_payables", "summarize_receivables"}:
            answer = _summary(d)
        elif tool == "search_suppliers":
            answer = _suppliers(d, plan.arguments)
        elif tool == "whoami":
            answer = _identity(d)
        else:
            answer = Answer(f"The {tool} tool returned a result this assistant cannot summarize.")
    if plan.identity_claim_ignored:
        answer.notes.insert(0, IDENTITY_NOTE)
    return answer
