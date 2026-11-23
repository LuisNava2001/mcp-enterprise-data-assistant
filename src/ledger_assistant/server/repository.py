"""Read-only data access. Every query is parameterized and every connection is opened read-only.

Connections use SQLite's `mode=ro` URI flag plus `PRAGMA query_only = ON`, so even a bug in this module
cannot write to the database. There is no code path that accepts SQL from a client.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from .access import Principal, RowScope

INVOICE_COLUMNS = """
  i.id, i.ledger, i.business_unit_id, i.invoice_number, i.issue_date, i.due_date, i.amount_cents, i.status, i.memo,
  COALESCE(i.supplier_id, i.customer_id) AS counterparty_id,
  COALESCE(s.name, c.name) AS counterparty_name,
  COALESCE((SELECT SUM(p.amount_cents) FROM payments p WHERE p.invoice_id = i.id), 0) AS paid_cents
"""
INVOICE_FROM = """
  FROM invoices i
  LEFT JOIN suppliers s ON s.id = i.supplier_id
  LEFT JOIN customers c ON c.id = i.customer_id
"""


def _scope_clause(scope: RowScope, params: list[Any]) -> str:
    clauses = []
    if scope.business_units is not None:
        units = sorted(scope.business_units)
        clauses.append(f"i.business_unit_id IN ({', '.join('?' for _ in units)})")
        params.extend(units)
    if scope.supplier_id is not None:
        clauses.append("i.supplier_id = ?")
        params.append(scope.supplier_id)
    return " AND ".join(clauses) if clauses else "1 = 1"


class Repository:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"dataset not found at {path}; run `ledger-assistant generate-data` first")
        self._uri = path.as_uri() + "?mode=ro"

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            yield conn
        finally:
            conn.close()

    # -- reference data -------------------------------------------------------

    def as_of(self) -> date:
        with self.connect() as conn:
            return date.fromisoformat(conn.execute("SELECT value FROM meta WHERE key = 'as_of_date'").fetchone()[0])

    def principal(self, subject: str) -> Principal | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM principals WHERE subject = ? AND active = 1", (subject,)).fetchone()
        if row is None:
            return None
        units = None if row["business_units"] == "*" else frozenset(row["business_units"].split(","))
        return Principal(
            subject=row["subject"],
            display_name=row["display_name"],
            role=row["role"],
            scopes=frozenset(row["allowed_scopes"].split()),
            business_units=units,
            supplier_id=row["supplier_id"],
        )

    def principals(self) -> list[Principal]:
        with self.connect() as conn:
            subjects = [r[0] for r in conn.execute("SELECT subject FROM principals WHERE active = 1 ORDER BY subject")]
        return [p for s in subjects if (p := self.principal(s))]

    # -- invoices -------------------------------------------------------------

    def invoice(self, invoice_id: str, scope: RowScope) -> sqlite3.Row | None:
        params: list[Any] = [invoice_id]
        sql = f"SELECT {INVOICE_COLUMNS} {INVOICE_FROM} WHERE i.id = ? AND {_scope_clause(scope, params)}"
        with self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    def payments(self, invoice_id: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT id, paid_on, amount_cents, method FROM payments WHERE invoice_id = ? ORDER BY paid_on, id",
                (invoice_id,),
            ).fetchall()

    def invoice_page(
        self,
        ledger: str,
        scope: RowScope,
        *,
        status: str | None = None,
        counterparty_id: str | None = None,
        business_unit_id: str | None = None,
        due_before: str | None = None,
        due_after: str | None = None,
        overdue_as_of: str | None = None,
        min_days_overdue: int = 0,
        after_key: list[str] | None = None,
        limit: int,
    ) -> tuple[list[sqlite3.Row], int]:
        """Rows ordered by (due_date, id) after `after_key`, plus the total number of matching rows."""
        params: list[Any] = [ledger]
        where = ["i.ledger = ?", _scope_clause(scope, params)]
        if status:
            where.append("i.status = ?")
            params.append(status)
        if counterparty_id:
            where.append("COALESCE(i.supplier_id, i.customer_id) = ?")
            params.append(counterparty_id)
        if business_unit_id:
            where.append("i.business_unit_id = ?")
            params.append(business_unit_id)
        if due_before:
            where.append("i.due_date < ?")
            params.append(due_before)
        if due_after:
            where.append("i.due_date > ?")
            params.append(due_after)
        if overdue_as_of:
            where.append("i.status <> 'PAID' AND julianday(?) - julianday(i.due_date) > ?")
            params.extend([overdue_as_of, min_days_overdue])
        filter_sql = " AND ".join(where)
        count_params = list(params)
        page_sql = filter_sql
        if after_key:
            page_sql += " AND (i.due_date > ? OR (i.due_date = ? AND i.id > ?))"
            params.extend([after_key[0], after_key[0], after_key[1]])
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) {INVOICE_FROM} WHERE {filter_sql}", count_params).fetchone()[0]
            rows = conn.execute(
                f"SELECT {INVOICE_COLUMNS} {INVOICE_FROM} WHERE {page_sql} ORDER BY i.due_date, i.id LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
        return rows, int(total)

    def summary_rows(self, ledger: str, scope: RowScope) -> list[sqlite3.Row]:
        params: list[Any] = [ledger]
        sql = (
            f"SELECT {INVOICE_COLUMNS} {INVOICE_FROM} WHERE i.ledger = ? AND {_scope_clause(scope, params)} "
            "ORDER BY i.id"
        )
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def supplier_tax_id(self, supplier_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT tax_id FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
        return row[0] if row else None

    def business_unit_names(self) -> dict[str, str]:
        with self.connect() as conn:
            return {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM business_units")}

    # -- suppliers ------------------------------------------------------------

    def supplier_page(
        self, query: str, business_units: frozenset[str] | None, after_key: list[str] | None, limit: int
    ) -> tuple[list[sqlite3.Row], int]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params: list[Any] = [f"%{escaped}%"]
        where = ["s.name LIKE ? ESCAPE '\\'"]
        if business_units is not None:
            units = sorted(business_units)
            where.append(f"s.business_unit_id IN ({', '.join('?' for _ in units)})")
            params.extend(units)
        filter_sql = " AND ".join(where)
        count_params = list(params)
        page_sql = filter_sql
        if after_key:
            page_sql += " AND (s.name > ? OR (s.name = ? AND s.id > ?))"
            params.extend([after_key[0], after_key[0], after_key[1]])
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM suppliers s WHERE {filter_sql}", count_params).fetchone()[0]
            rows = conn.execute(
                "SELECT s.id, s.name, s.country, s.business_unit_id, s.payment_terms_days, s.tax_id "
                f"FROM suppliers s WHERE {page_sql} ORDER BY s.name, s.id LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
        return rows, int(total)
