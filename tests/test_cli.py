from __future__ import annotations

from pathlib import Path

import pytest

from ledger_assistant.cli import main


def test_generate_data(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "ledger.db"
    assert main(["generate-data", "--db", str(target)]) == 0
    assert target.is_file()
    assert "Synthetic dataset" in capsys.readouterr().out


def test_serve_refuses_non_loopback_hosts(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="loopback"):
        main(["serve", "--host", "0.0.0.0", "--db", str(tmp_path / "x.db")])


def test_token_needs_the_shared_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_TOKEN_SECRET", raising=False)
    with pytest.raises(SystemExit, match="LEDGER_TOKEN_SECRET"):
        main(["token", "--subject", "ava.ap"])


def test_token_mints_a_jwt(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LEDGER_TOKEN_SECRET", "t" * 40)
    assert main(["token", "--subject", "ava.ap"]) == 0
    assert capsys.readouterr().out.count(".") == 2


def test_ask_embedded_runs_a_conversation(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ask", "--embedded", "--subject", "ava.ap", "Has invoice AP-100001 been paid?", "Mark it as paid"]) == 0
    out = capsys.readouterr().out
    assert "Mode: Demo Mode - deterministic rule-based planner, no LLM" in out
    assert "Tool: get_invoice_payment_status" in out
    assert "$7,500.00" in out
    assert "refusal: unsupported_operation" in out


def test_ask_reports_unreachable_server(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LEDGER_ACCESS_TOKEN", "x")
    assert main(["ask", "--url", "http://127.0.0.1:9/mcp", "Who am I?"]) == 2
    assert "Connection failed" in capsys.readouterr().err
