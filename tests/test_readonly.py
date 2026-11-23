import ast
import sqlite3
from pathlib import Path

import pytest

from ledger_assistant.server.repository import Repository

from .helpers import REPO_ROOT


def test_repository_connections_cannot_write(repository: Repository) -> None:
    with repository.connect() as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO meta VALUES ('x', 'y')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE invoices SET status = 'PAID'")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DROP TABLE invoices")


def test_read_only_flag_holds_even_without_the_pragma(db_path: Path) -> None:
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM payments")
    finally:
        conn.close()


def test_missing_dataset_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="generate-data"):
        Repository(tmp_path / "absent.db")


def test_server_sql_is_never_built_from_tool_input() -> None:
    """f-strings in the repository may only interpolate fixed SQL fragments, placeholder lists or bound values.

    Interpolating a tool argument such as `query` or `status` directly into SQL would fail this test.
    """
    source = (REPO_ROOT / "src/ledger_assistant/server/repository.py").read_text(encoding="utf-8")
    allowed = {
        "INVOICE_COLUMNS", "INVOICE_FROM", "filter_sql", "page_sql", "sql",  # fixed fragments built in this module
        "_scope_clause", "scope", "params",  # row filter from the verified principal, rendered as placeholders
        "join", "units", "_",  # "?, ?, ?" placeholder lists
        "escaped",  # LIKE pattern passed as a bound parameter, not SQL
        "path",  # error message only
    }
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    names = {n.id for n in ast.walk(value.value) if isinstance(n, ast.Name)}
                    names |= {n.attr for n in ast.walk(value.value) if isinstance(n, ast.Attribute)}
                    assert names <= allowed, f"unexpected interpolation {names - allowed}"
