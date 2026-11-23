import sqlite3
from pathlib import Path

from ledger_assistant.data.generate import AS_OF, COMPANY, dump, generate


def test_generation_is_reproducible(tmp_path: Path) -> None:
    first = dump(generate(tmp_path / "a.db"))
    second = dump(generate(tmp_path / "b.db"))
    assert first == second
    assert len(first.splitlines()) > 1000


def test_dataset_shape_and_anchors(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        counts = dict(conn.execute("SELECT ledger, COUNT(*) FROM invoices GROUP BY ledger").fetchall())
        assert counts == {"AP": 420, "AR": 300}
        assert conn.execute("SELECT value FROM meta WHERE key = 'company'").fetchone()[0] == COMPANY
        assert conn.execute("SELECT value FROM meta WHERE key = 'as_of_date'").fetchone()[0] == AS_OF.isoformat()
        row = conn.execute("SELECT supplier_id, amount_cents, status FROM invoices WHERE id = 'AP-100001'").fetchone()
        assert row == ("SUP-0007", 1_250_000, "PARTIALLY_PAID")
        paid = conn.execute("SELECT SUM(amount_cents) FROM payments WHERE invoice_id = 'AP-100001'").fetchone()[0]
        assert paid == 500_000
        assert conn.execute("SELECT COUNT(*) FROM principals WHERE active = 1").fetchone()[0] == 5
        # Every payment total stays within its invoice amount.
        over = conn.execute(
            "SELECT COUNT(*) FROM invoices i JOIN (SELECT invoice_id, SUM(amount_cents) s FROM payments GROUP BY invoice_id) p "
            "ON p.invoice_id = i.id WHERE p.s > i.amount_cents").fetchone()[0]
        assert over == 0
    conn.close()


def test_company_and_identifiers_are_fictional(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        assert "fictional" in COMPANY
        accounts = [r[0] for r in conn.execute("SELECT bank_account FROM suppliers")]
        assert all(a.startswith("ACCT-") for a in accounts)
        taxes = [r[0] for r in conn.execute("SELECT tax_id FROM suppliers")]
        assert all(t.startswith("TX-") for t in taxes)
    conn.close()
