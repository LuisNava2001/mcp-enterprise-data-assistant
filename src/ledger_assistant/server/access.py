"""Access policy: who may call which tool, which rows they see and which fields are masked.

The identity always comes from a verified bearer token, never from tool arguments or from text the
model produced. Out-of-scope records are reported as NOT_FOUND so the API does not confirm they exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .errors import ErrorCode, tool_error


class Scope(StrEnum):
    ACCESS = "ledger:access"
    AP_READ = "ap:read"
    AP_READ_OWN = "ap:read:own"
    AR_READ = "ar:read"
    SUPPLIERS_READ = "suppliers:read"
    SENSITIVE_READ = "sensitive:read"


@dataclass(frozen=True)
class Principal:
    subject: str
    display_name: str
    role: str
    scopes: frozenset[str]
    business_units: frozenset[str] | None  # None means every business unit
    supplier_id: str | None  # set for supplier-portal users: they see only that supplier's invoices

    def has(self, scope: Scope) -> bool:
        return scope.value in self.scopes


@dataclass(frozen=True)
class RowScope:
    """Row filter applied inside every SQL query."""

    business_units: frozenset[str] | None
    supplier_id: str | None

    def describe(self) -> str:
        if self.supplier_id:
            return f"invoices of supplier {self.supplier_id} only"
        if self.business_units is None:
            return "all business units"
        return "business units " + ", ".join(sorted(self.business_units))


def ledger_scope(principal: Principal, ledger: str, *, aggregate: bool = False) -> RowScope:
    """Row scope for a ledger, or FORBIDDEN. Aggregates need full read access, not portal access."""
    if ledger == "AP":
        if principal.has(Scope.AP_READ):
            return RowScope(principal.business_units, None)
        if principal.has(Scope.AP_READ_OWN) and principal.supplier_id and not aggregate:
            return RowScope(None, principal.supplier_id)
        raise tool_error(ErrorCode.FORBIDDEN, f"Your access does not include {'payables summaries' if aggregate else 'accounts payable'}.")
    if principal.has(Scope.AR_READ):
        return RowScope(principal.business_units, None)
    raise tool_error(ErrorCode.FORBIDDEN, "Your access does not include accounts receivable.")


def supplier_directory_scope(principal: Principal) -> frozenset[str] | None:
    if not principal.has(Scope.SUPPLIERS_READ):
        raise tool_error(ErrorCode.FORBIDDEN, "Your access does not include the supplier directory.")
    return principal.business_units


def check_business_unit(principal: Principal, business_unit_id: str | None) -> None:
    if business_unit_id and principal.business_units is not None and business_unit_id not in principal.business_units:
        raise tool_error(ErrorCode.FORBIDDEN, f"Business unit {business_unit_id} is outside your access.")


def check_counterparty(principal: Principal, scope: RowScope, counterparty_id: str | None) -> None:
    if counterparty_id and scope.supplier_id and counterparty_id != scope.supplier_id:
        raise tool_error(ErrorCode.FORBIDDEN, "Supplier portal users can only view their own invoices.")


def mask_tax_id(tax_id: str, principal: Principal) -> tuple[str, bool]:
    """Return (value, masked). Only callers with sensitive:read see the full tax identifier."""
    if principal.has(Scope.SENSITIVE_READ):
        return tax_id, False
    return f"{tax_id[:3]}****{tax_id[-3:]}", True
