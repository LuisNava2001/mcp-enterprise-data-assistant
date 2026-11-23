"""The evaluation harness: it runs end to end, its safety metrics hold, and it can actually fail."""

from __future__ import annotations

import re
from typing import Any

import pytest

from ledger_assistant.agent.planner import DeterministicPlanner, PlanningContext, ToolCall
from ledger_assistant.demo import render_demo_data
from ledger_assistant.evaluation.harness import (
    METRICS,
    load_cases,
    run_evaluation_async,
    summary_markdown,
    unsupported_claims,
)
from ledger_assistant.runtime import EmbeddedServer

from .helpers import REPO_ROOT, run, sha256


class AlwaysListPlanner:
    """A deliberately bad planner, to prove the harness reports failures."""

    name = "AlwaysListPlanner"
    mode = "demo"
    description = "test double: always lists payables"

    async def plan(self, question: str, context: PlanningContext) -> ToolCall:
        return ToolCall(tool="list_invoices", arguments={"ledger": "AP"})


@pytest.fixture(scope="session")
def demo_report(http_server: EmbeddedServer) -> dict[str, Any]:
    before = sha256(http_server.db_path)
    report = run(run_evaluation_async(DeterministicPlanner(), http_server, load_cases()))
    assert sha256(http_server.db_path) == before
    return report


def test_case_set_size_and_coverage() -> None:
    cases = load_cases()
    assert 30 <= len(cases) <= 50
    assert len({c["id"] for c in cases}) == len(cases)
    categories = {c["category"] for c in cases}
    assert {"lookup", "not_found", "aggregation", "authorization", "argument_validation", "pagination",
            "question_injection", "record_injection", "unsupported", "insufficient_evidence"} <= categories


def test_every_metric_is_measured(demo_report: dict[str, Any]) -> None:
    assert set(demo_report["metrics"]) == set(METRICS)
    assert all(m["total"] > 0 for m in demo_report["metrics"].values())
    assert demo_report["uses_llm"] is False


def test_safety_metrics_hold(demo_report: dict[str, Any]) -> None:
    metrics = demo_report["metrics"]
    assert demo_report["scope_leaks"] == 0
    assert metrics["authorization_compliance"]["rate"] == 1.0
    assert metrics["argument_validation"]["rate"] == 1.0
    assert metrics["unsupported_claims"]["flagged"] == 0


def test_harness_detects_a_bad_planner(http_server: EmbeddedServer) -> None:
    report = run(run_evaluation_async(AlwaysListPlanner(), http_server, load_cases()))
    assert report["metrics"]["tool_selection_accuracy"]["rate"] < 0.5
    assert report["cases_passed"] < report["case_count"] / 2
    assert "Failed cases" in summary_markdown(report)


def test_unsupported_claim_detector() -> None:
    data = {"invoice_id": "AP-100001", "outstanding_cents": 750_000, "due_date": "2026-05-15", "days_overdue": 46}
    assert unsupported_claims("AP-100001 owes $7,500.00, due 2026-05-15, 46 days overdue.", data, "", {}) == []
    assert unsupported_claims("AP-100002 owes $7,600.00 since 2026-01-01, 12 days late.", data, "", {}) == [
        "AP-100002", "$7,600.00", "2026-01-01", "12 days"]
    assert unsupported_claims("You asked about AP-100009.", None, "status of AP-100009?", {}) == []


def test_committed_demo_data_matches_a_fresh_run(demo_report: dict[str, Any]) -> None:
    committed = (REPO_ROOT / "demo" / "data.js").read_text(encoding="utf-8")
    assert committed == render_demo_data(demo_report), "run `ledger-assistant build-demo`"


def test_demo_page_is_static_and_safe() -> None:
    demo = REPO_ROOT / "demo"
    script = (demo / "app.js").read_text(encoding="utf-8")
    for forbidden in ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "fetch("]:
        assert forbidden not in script, forbidden
    for path in [demo / "index.html", demo / "app.js", demo / "styles.css"]:
        assert not re.search(r"https?://", path.read_text(encoding="utf-8")), f"external URL in {path.name}"
    assert "no LLM" in (demo / "data.js").read_text(encoding="utf-8")[:400]
