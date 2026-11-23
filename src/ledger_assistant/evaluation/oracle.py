"""Independent ground truth for the evaluation.

Written separately from server/repository.py and server/access.py: it reads the principals table directly and
re-derives visibility, overdue days and totals with its own SQL, so a bug in the server is not mirrored here.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

PAGE = 20


class Oracle:
    def __init__(self, db_path: Path) -> None:
        self._uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with self._conn() as conn:
            self.as_of = conn.execute("SELECT value FROM meta WHERE key = 'as_of_date'").fetchone()[0]
            self.principals = {row["subject"]: dict(row) for row in conn.execute("SELECT * FROM principals")}

    def _conn(self) -> closing[sqlite3.Connection]:
        conn = sqlite3.connect(self._uri, uri=True)
        conn.row_factory = sqlite3.Row
        return closing(conn)

    # -- visibility -------------------------------------------------------------

    def _visibility(self, subject: str, ledger: str) -> tuple[str, list[Any]] | None:
        """SQL predicate on alias v for rows the principal may see in a ledger, or None if the ledger is forbidden."""
        p = self.principals[subject]
        scopes = set(p["allowed_scopes"].split())
        units = None if p["business_units"] == "*" else p["business_units"].split(",")
        unit_sql = ("v.business_unit_id IN (" + ",".join("?" * len(units)) + ")", list(units)) if units else ("1=1", [])
        if ledger == "AP" and "ap:read" in scopes:
            return unit_sql
        if ledger == "AP" and "ap:read:own" in scopes and p["supplier_id"]:
            return "v.supplier_id = ?", [p["supplier_id"]]
        if ledger == "AR" and "ar:read" in scopes:
            return unit_sql
        return None

    def _rows(self, subject: str, ledger: str, extra: str = "1=1", params: list[Any] | None = None) -> list[dict[str, Any]]:
        vis = self._visibility(subject, ledger)
        if vis is None:
            return []
        sql = (
            "SELECT v.*, IFNULL(pt.paid, 0) AS paid_cents, "
            "  CAST(julianday(?) - julianday(v.due_date) AS INTEGER) AS age_days "
            "FROM invoices v LEFT JOIN (SELECT invoice_id, SUM(amount_cents) AS paid FROM payments GROUP BY invoice_id) pt "
            "  ON pt.invoice_id = v.id "
            f"WHERE v.ledger = ? AND ({vis[0]}) AND ({extra}) ORDER BY v.due_date, v.id"
        )
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(sql, [self.as_of, ledger, *vis[1], *(params or [])])]
        for row in rows:
            row["outstanding_cents"] = max(row["amount_cents"] - row["paid_cents"], 0)
            row["days_overdue"] = row["age_days"] if row["outstanding_cents"] > 0 and row["age_days"] > 0 else 0
        return rows

    def visible_invoice_ids(self, subject: str) -> set[str]:
        return {row["id"] for ledger in ("AP", "AR") for row in self._rows(subject, ledger)}

    def visible_supplier_ids(self, subject: str) -> set[str]:
        p = self.principals[subject]
        if "suppliers:read" not in p["allowed_scopes"].split():
            return set()
        with self._conn() as conn:
            rows = conn.execute("SELECT id, business_unit_id FROM suppliers").fetchall()
        units = None if p["business_units"] == "*" else set(p["business_units"].split(","))
        return {r["id"] for r in rows if units is None or r["business_unit_id"] in units}

    # -- expectations -------------------------------------------------------------

    def payment_status(self, subject: str, invoice_id: str) -> dict[str, Any] | None:
        rows = self._rows(subject, invoice_id[:2], "v.id = ?", [invoice_id])
        if not rows:
            return None
        r = rows[0]
        return {key: r[key] for key in ("status", "amount_cents", "paid_cents", "outstanding_cents", "days_overdue", "due_date")}

    def invoice_detail(self, subject: str, invoice_id: str) -> dict[str, Any] | None:
        status = self.payment_status(subject, invoice_id)
        if status is None:
            return None
        with self._conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM payments WHERE invoice_id = ?", (invoice_id,)).fetchone()[0]
            party = conn.execute(
                "SELECT COALESCE(s.name, c.name) FROM invoices v LEFT JOIN suppliers s ON s.id = v.supplier_id "
                "LEFT JOIN customers c ON c.id = v.customer_id WHERE v.id = ?", (invoice_id,)).fetchone()[0]
        return {**status, "payment_count": count, "counterparty_name": party}

    def invoice_page(self, subject: str, ledger: str, filters: dict[str, Any], page: int = 1) -> dict[str, Any]:
        clauses, params = [], []
        column = {"status": "v.status = ?", "business_unit_id": "v.business_unit_id = ?",
                  "counterparty_id": "IFNULL(v.supplier_id, v.customer_id) = ?",
                  "due_before": "v.due_date < ?", "due_after": "v.due_date > ?"}
        for key, value in filters.items():
            clauses.append(column[key])
            params.append(value)
        rows = self._rows(subject, ledger, " AND ".join(clauses) or "1=1", params)
        return self._paged(rows, page)

    def overdue_page(self, subject: str, ledger: str, min_days: int, page: int = 1) -> dict[str, Any]:
        rows = [r for r in self._rows(subject, ledger) if r["status"] != "PAID" and r["age_days"] > min_days]
        return self._paged(rows, page)

    @staticmethod
    def _paged(rows: list[dict[str, Any]], page: int) -> dict[str, Any]:
        chunk = rows[(page - 1) * PAGE: page * PAGE]
        return {"total_matching": len(rows), "ids": [r["id"] for r in chunk], "has_more": len(rows) > page * PAGE}

    def summary(self, subject: str, ledger: str, group_by: str) -> dict[str, Any]:
        rows = self._rows(subject, ledger)
        key_of = {"status": "status", "business_unit": "business_unit_id"}.get(group_by)
        groups: dict[str, int] = {}
        for r in rows:
            key = r[key_of] if key_of else (r["supplier_id"] or r["customer_id"])
            groups[key] = groups.get(key, 0) + r["outstanding_cents"]
        return {
            "invoice_count": len(rows),
            "amount_cents": sum(r["amount_cents"] for r in rows),
            "outstanding_cents": sum(r["outstanding_cents"] for r in rows),
            "overdue_cents": sum(r["outstanding_cents"] for r in rows if r["days_overdue"] > 0),
            "group_keys": sorted(groups, key=lambda k: (-groups[k], k)),
        }

    def supplier_search(self, subject: str, query: str) -> dict[str, Any]:
        visible = self.visible_supplier_ids(subject)
        with self._conn() as conn:
            rows = conn.execute("SELECT id, name FROM suppliers ORDER BY name, id").fetchall()
        hits = [r["id"] for r in rows if r["id"] in visible and query.lower() in r["name"].lower()]
        return {"total_matching": len(hits), "ids": hits[:PAGE]}
