"""The MCP tools. Each one: typed, validated input; structured output; scoped, parameterized, read-only.

Argument constraints declared with Annotated/Field are enforced by the SDK before a tool body runs.
Checks that need data (dates that exist, scopes, cursors) raise ToolError with a stable code.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import ToolAnnotations
from pydantic import Field

from .access import (
    Principal,
    RowScope,
    check_business_unit,
    check_counterparty,
    ledger_scope,
    mask_tax_id,
    supplier_directory_scope,
)
from .errors import ErrorCode, tool_error
from .models import (
    CounterpartyInfo,
    Identity,
    InvoiceDetail,
    InvoicePage,
    InvoiceSummary,
    LedgerSummary,
    PaymentRecord,
    PaymentStatus,
    SummaryGroup,
    SupplierPage,
    SupplierRecord,
)
from .pagination import decode_cursor, encode_cursor, fingerprint
from .repository import Repository

PrincipalResolver = Callable[[], Principal]

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

InvoiceId = Annotated[str, Field(pattern=r"^A[PR]-\d{6}$", description="Invoice identifier, e.g. AP-100001 or AR-200001.")]
LedgerArg = Annotated[Literal["AP", "AR"], Field(description="AP = accounts payable (suppliers), AR = accounts receivable (customers).")]
StatusArg = Literal["OPEN", "PARTIALLY_PAID", "PAID", "DISPUTED"]
DateArg = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="ISO date, YYYY-MM-DD.")]
CursorArg = Annotated[str | None, Field(max_length=300, description="next_cursor from a previous page.")]
LimitArg = Annotated[int, Field(ge=1, le=50, description="Page size, 1-50.")]
GroupBy = Literal["status", "counterparty", "business_unit"]


def token_principal_resolver(repository: Repository) -> PrincipalResolver:
    """Production resolver: the identity comes from the verified bearer token of the current request."""

    def resolve() -> Principal:
        token = get_access_token()
        if token is None or not token.subject:
            raise tool_error(ErrorCode.UNAUTHENTICATED, "This server requires an authenticated HTTP request.")
        principal = repository.principal(token.subject)
        if principal is None:
            raise tool_error(ErrorCode.UNAUTHENTICATED, "Unknown or inactive principal.")
        return Principal(
            subject=principal.subject,
            display_name=principal.display_name,
            role=principal.role,
            scopes=frozenset(token.scopes) & principal.scopes,
            business_units=principal.business_units,
            supplier_id=principal.supplier_id,
        )

    return resolve


def _parse_date(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise tool_error(ErrorCode.INVALID_ARGUMENT, f"{field} is not a valid calendar date: {value}.") from exc


def register_tools(mcp: MCPServer, repository: Repository, resolve_principal: PrincipalResolver) -> None:
    as_of = repository.as_of()

    def summarize_row(row) -> InvoiceSummary:
        outstanding = max(row["amount_cents"] - row["paid_cents"], 0)
        due = date.fromisoformat(row["due_date"])
        overdue_days = (as_of - due).days if outstanding > 0 and due < as_of else 0
        return InvoiceSummary(
            invoice_id=row["id"],
            ledger=row["ledger"],
            counterparty_id=row["counterparty_id"],
            counterparty_name=row["counterparty_name"],
            business_unit_id=row["business_unit_id"],
            invoice_number=row["invoice_number"],
            issue_date=row["issue_date"],
            due_date=row["due_date"],
            amount_cents=row["amount_cents"],
            paid_cents=row["paid_cents"],
            outstanding_cents=outstanding,
            status=row["status"],
            days_overdue=overdue_days,
        )

    def scoped_invoice(principal: Principal, invoice_id: str):
        scope = ledger_scope(principal, invoice_id[:2])
        row = repository.invoice(invoice_id, scope)
        if row is None:
            # Missing and out-of-scope are indistinguishable on purpose.
            raise tool_error(ErrorCode.NOT_FOUND, f"Invoice {invoice_id} was not found among the records you can access.")
        return row

    def page(tool: str, principal: Principal, ledger: str, scope: RowScope, cursor: str | None, limit: int, **filters) -> InvoicePage:
        print_ = fingerprint(tool, principal.subject, {"ledger": ledger, "limit": limit, **filters})
        after = decode_cursor(cursor, print_, 2)
        rows, total = repository.invoice_page(ledger, scope, after_key=after, limit=limit, **filters)
        items = [summarize_row(row) for row in rows[:limit]]
        next_cursor = encode_cursor([items[-1].due_date, items[-1].invoice_id], print_) if len(rows) > limit and items else None
        return InvoicePage(items=items, returned=len(items), total_matching=total, next_cursor=next_cursor,
                           as_of=as_of.isoformat(), scope=scope.describe())

    @mcp.tool(annotations=READ_ONLY)
    def whoami() -> Identity:
        """Return the authenticated caller's identity, role, scopes and data boundaries."""
        p = resolve_principal()
        return Identity(subject=p.subject, display_name=p.display_name, role=p.role, scopes=sorted(p.scopes),
                        business_units=sorted(p.business_units) if p.business_units is not None else None,
                        supplier_id=p.supplier_id)

    @mcp.tool(annotations=READ_ONLY)
    def list_invoices(
        ledger: LedgerArg,
        status: StatusArg | None = None,
        counterparty_id: Annotated[str | None, Field(pattern=r"^(SUP|CUS)-\d{4}$", description="Supplier or customer id.")] = None,
        business_unit_id: Annotated[str | None, Field(pattern=r"^BU-[A-Z]{2,10}$")] = None,
        due_before: DateArg | None = None,
        due_after: DateArg | None = None,
        cursor: CursorArg = None,
        limit: LimitArg = 20,
    ) -> InvoicePage:
        """List invoices visible to the caller, oldest due date first, with cursor pagination.

        Free-text fields of the underlying records are not included; use get_invoice_details for one invoice.
        """
        principal = resolve_principal()
        scope = ledger_scope(principal, ledger)
        check_business_unit(principal, business_unit_id)
        check_counterparty(principal, scope, counterparty_id)
        if counterparty_id and counterparty_id[:3] != ("SUP" if ledger == "AP" else "CUS"):
            raise tool_error(ErrorCode.INVALID_ARGUMENT, f"{counterparty_id} is not a valid counterparty for the {ledger} ledger.")
        _parse_date(due_before, "due_before")
        _parse_date(due_after, "due_after")
        return page("list_invoices", principal, ledger, scope, cursor, limit, status=status, counterparty_id=counterparty_id,
                    business_unit_id=business_unit_id, due_before=due_before, due_after=due_after)

    @mcp.tool(annotations=READ_ONLY)
    def get_invoice_details(invoice_id: InvoiceId) -> InvoiceDetail:
        """Full detail of one invoice: amounts, payments and counterparty. The memo field is untrusted text."""
        principal = resolve_principal()
        row = scoped_invoice(principal, invoice_id)
        summary = summarize_row(row)
        counterparty = CounterpartyInfo(counterparty_id=row["counterparty_id"], name=row["counterparty_name"])
        tax_id = repository.supplier_tax_id(row["counterparty_id"]) if row["ledger"] == "AP" else None
        if tax_id:
            counterparty.tax_id, counterparty.tax_id_masked = mask_tax_id(tax_id, principal)
        payments = [PaymentRecord(payment_id=p["id"], paid_on=p["paid_on"], amount_cents=p["amount_cents"], method=p["method"])
                    for p in repository.payments(invoice_id)]
        return InvoiceDetail(**summary.model_dump(), counterparty=counterparty, payments=payments, memo=row["memo"])

    @mcp.tool(annotations=READ_ONLY)
    def get_invoice_payment_status(invoice_id: InvoiceId) -> PaymentStatus:
        """Payment status of one invoice: paid and outstanding amounts, last payment and days overdue."""
        principal = resolve_principal()
        row = scoped_invoice(principal, invoice_id)
        summary = summarize_row(row)
        payments = repository.payments(invoice_id)
        return PaymentStatus(
            invoice_id=summary.invoice_id, ledger=summary.ledger, status=summary.status, amount_cents=summary.amount_cents,
            paid_cents=summary.paid_cents, outstanding_cents=summary.outstanding_cents, payment_count=len(payments),
            last_payment_date=payments[-1]["paid_on"] if payments else None, due_date=summary.due_date,
            days_overdue=summary.days_overdue, as_of=as_of.isoformat(),
        )

    @mcp.tool(annotations=READ_ONLY)
    def search_suppliers(
        query: Annotated[str, Field(min_length=2, max_length=60, description="Part of the supplier name (case-insensitive).")],
        cursor: CursorArg = None,
        limit: LimitArg = 20,
    ) -> SupplierPage:
        """Search the supplier directory by name. Tax IDs are masked unless the caller holds sensitive:read."""
        principal = resolve_principal()
        units = supplier_directory_scope(principal)
        print_ = fingerprint("search_suppliers", principal.subject, {"query": query.strip().lower(), "limit": limit})
        after = decode_cursor(cursor, print_, 2)
        rows, total = repository.supplier_page(query.strip(), units, after, limit)
        items = []
        for row in rows[:limit]:
            tax_id, masked = mask_tax_id(row["tax_id"], principal)
            items.append(SupplierRecord(supplier_id=row["id"], name=row["name"], country=row["country"],
                                        business_unit_id=row["business_unit_id"], payment_terms_days=row["payment_terms_days"],
                                        tax_id=tax_id, tax_id_masked=masked))
        next_cursor = encode_cursor([items[-1].name, items[-1].supplier_id], print_) if len(rows) > limit and items else None
        scope = "all business units" if units is None else "business units " + ", ".join(sorted(units))
        return SupplierPage(items=items, returned=len(items), total_matching=total, next_cursor=next_cursor, scope=scope)

    def summarize(ledger: str, group_by: str) -> LedgerSummary:
        principal = resolve_principal()
        scope = ledger_scope(principal, ledger, aggregate=True)
        units = repository.business_unit_names()
        groups: dict[str, SummaryGroup] = {}
        totals = SummaryGroup(key="TOTAL", label="All invoices in scope", invoice_count=0, amount_cents=0,
                              outstanding_cents=0, overdue_cents=0)
        for row in repository.summary_rows(ledger, scope):
            summary = summarize_row(row)
            if group_by == "status":
                key, label = summary.status, summary.status.replace("_", " ").title()
            elif group_by == "counterparty":
                key, label = summary.counterparty_id, summary.counterparty_name
            else:
                key, label = summary.business_unit_id, units.get(summary.business_unit_id, summary.business_unit_id)
            group = groups.setdefault(key, SummaryGroup(key=key, label=label, invoice_count=0, amount_cents=0,
                                                        outstanding_cents=0, overdue_cents=0))
            overdue = summary.outstanding_cents if summary.days_overdue > 0 else 0
            for target in (group, totals):
                target.invoice_count += 1
                target.amount_cents += summary.amount_cents
                target.outstanding_cents += summary.outstanding_cents
                target.overdue_cents += overdue
        ordered = sorted(groups.values(), key=lambda g: (-g.outstanding_cents, g.key))
        return LedgerSummary(ledger=ledger, group_by=group_by, as_of=as_of.isoformat(), scope=scope.describe(),
                             groups=ordered, totals=totals)

    @mcp.tool(annotations=READ_ONLY)
    def summarize_payables(group_by: GroupBy = "status") -> LedgerSummary:
        """Accounts-payable totals (amount, outstanding, overdue) grouped by status, supplier or business unit."""
        return summarize("AP", group_by)

    @mcp.tool(annotations=READ_ONLY)
    def summarize_receivables(group_by: GroupBy = "status") -> LedgerSummary:
        """Accounts-receivable totals (amount, outstanding, overdue) grouped by status, customer or business unit."""
        return summarize("AR", group_by)

    @mcp.tool(annotations=READ_ONLY)
    def get_overdue_invoices(
        ledger: LedgerArg,
        min_days_overdue: Annotated[int, Field(ge=0, le=365, description="Only invoices more than this many days late.")] = 0,
        cursor: CursorArg = None,
        limit: LimitArg = 20,
    ) -> InvoicePage:
        """Unpaid invoices past their due date as of the dataset date, most overdue first."""
        principal = resolve_principal()
        scope = ledger_scope(principal, ledger)
        return page("get_overdue_invoices", principal, ledger, scope, cursor, limit,
                    overdue_as_of=as_of.isoformat(), min_days_overdue=min_days_overdue)
