"""Run the evaluation cases end to end and score them with deterministic checks.

Each case opens its own MCP session over real Streamable HTTP with a token for the case's principal. No model is
used for grading. Scores are whatever the run produces; nothing is hard-coded.
"""

from __future__ import annotations

import json
import re
import time
from importlib import resources
from pathlib import Path
from typing import Any

from ..agent.orchestrator import Orchestrator, Trace
from ..agent.planner import Planner
from ..client.session import ToolOutcome, connect_http
from ..runtime import EmbeddedServer
from .oracle import Oracle

ID_RX = re.compile(r"\b(?:AP|AR)-\d{6}\b|\b(?:SUP|CUS)-\d{4}\b")
MONEY_RX = re.compile(r"-?\$[\d,]+\.\d{2}")
DATE_RX = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
DAYS_RX = re.compile(r"\b(\d+) days\b")

METRICS = {
    "tool_selection_accuracy": "Planner chose the expected tool (or refused when it should).",
    "argument_accuracy": "Planner arguments equal the expected arguments (cursor excluded), when the tool was right.",
    "argument_validation": "Invalid or extra arguments were rejected with INVALID_ARGUMENT; valid edge input was accepted.",
    "data_retrieval_accuracy": "Tool output matches the independent SQL oracle.",
    "authorization_compliance": "Expected deny / not-found / masking outcome held and no out-of-scope data appeared.",
    "unsupported_claims": "Answers containing an identifier, amount, date or day count absent from the evidence (lower is better).",
    "response_grounding": "Answer contains the required facts and every cited evidence id is present in the tool output.",
}


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8") if path else resources.files(__package__).joinpath("cases.json").read_text(encoding="utf-8")
    return json.loads(text)["cases"]


def _flatten(value: Any, strings: list[str], ints: set[int]) -> None:
    if isinstance(value, dict):
        for inner in value.values():
            _flatten(inner, strings, ints)
    elif isinstance(value, list):
        for inner in value:
            _flatten(inner, strings, ints)
    elif isinstance(value, bool):
        return
    elif isinstance(value, int):
        ints.add(value)
    elif isinstance(value, str):
        strings.append(value)


def unsupported_claims(answer: str, data: Any, question: str, arguments: dict[str, Any] | None) -> list[str]:
    """Factual tokens in the answer that are neither in the tool output nor echoed from the question/arguments."""
    strings: list[str] = []
    ints: set[int] = set()
    _flatten(data, strings, ints)
    blob = " ".join(strings)
    echoed = question + " " + json.dumps(arguments or {})
    bad = [token for token in ID_RX.findall(answer) if token not in blob and token not in echoed]
    for amount in MONEY_RX.findall(answer):
        if round(float(amount.replace("$", "").replace(",", "")) * 100) not in ints:
            bad.append(amount)
    bad += [day for day in DATE_RX.findall(answer) if day not in blob and day not in echoed]
    bad += [f"{n} days" for n in DAYS_RX.findall(answer) if int(n) not in ints and n not in echoed]
    return bad


def scope_leaks(principal: str, data: Any, oracle: Oracle) -> list[str]:
    """Out-of-scope rows, unmasked tax ids for callers without sensitive:read, or bank details in any output."""
    if not isinstance(data, dict):
        return []
    leaks: list[str] = []
    visible = oracle.visible_invoice_ids(principal)
    sensitive = "sensitive:read" in oracle.principals[principal]["allowed_scopes"].split()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "bank_account" in node:
                leaks.append("bank_account field present")
            if "invoice_id" in node and node["invoice_id"] not in visible:
                leaks.append(f"invoice {node['invoice_id']} outside scope")
            if node.get("tax_id") and not sensitive and "****" not in node["tax_id"]:
                leaks.append(f"unmasked tax id {node['tax_id']}")
            for inner in node.values():
                walk(inner)
        elif isinstance(node, list):
            for inner in node:
                walk(inner)

    walk(data)
    if "items" in data and data["items"] and "supplier_id" in data["items"][0]:
        allowed = oracle.visible_supplier_ids(principal)
        leaks += [f"supplier {item['supplier_id']} outside scope" for item in data["items"] if item["supplier_id"] not in allowed]
    return leaks


def matches_oracle(check: dict[str, Any], principal: str, data: dict[str, Any], oracle: Oracle) -> bool:
    kind = check["kind"]
    if kind == "payment_status":
        expected = oracle.payment_status(principal, check["invoice_id"])
        return expected is not None and all(data[key] == value for key, value in expected.items())
    if kind == "invoice_detail":
        expected = oracle.invoice_detail(principal, check["invoice_id"])
        if expected is None:
            return False
        return (all(data[key] == expected[key] for key in ("status", "amount_cents", "paid_cents", "outstanding_cents",
                                                          "days_overdue", "due_date"))
                and len(data["payments"]) == expected["payment_count"]
                and data["counterparty"]["name"] == expected["counterparty_name"])
    if kind in {"invoice_page", "overdue_page"}:
        if kind == "invoice_page":
            expected = oracle.invoice_page(principal, check["ledger"], check["filters"], check["page"])
        else:
            expected = oracle.overdue_page(principal, check["ledger"], check["min_days"], check["page"])
        return (data["total_matching"] == expected["total_matching"]
                and [item["invoice_id"] for item in data["items"]] == expected["ids"]
                and (data["next_cursor"] is not None) == expected["has_more"])
    if kind == "summary":
        expected = oracle.summary(principal, check["ledger"], check["group_by"])
        totals = data["totals"]
        return (all(totals[key] == expected[key] for key in ("invoice_count", "amount_cents", "outstanding_cents", "overdue_cents"))
                and [group["key"] for group in data["groups"]] == expected["group_keys"])
    if kind == "supplier_search":
        expected = oracle.supplier_search(principal, check["query"])
        return data["total_matching"] == expected["total_matching"] and [i["supplier_id"] for i in data["items"]] == expected["ids"]
    if kind == "identity":
        row = oracle.principals[principal]
        units = None if row["business_units"] == "*" else sorted(row["business_units"].split(","))
        return (data["subject"] == principal and data["role"] == row["role"] and data["business_units"] == units
                and sorted(data["scopes"]) == sorted(row["allowed_scopes"].split()) and data["supplier_id"] == row["supplier_id"])
    raise ValueError(f"unknown check kind {kind}")


def _outcome_label(outcome: dict[str, Any] | None) -> str | None:
    if outcome is None:
        return None
    return "ok" if outcome["ok"] else outcome["error_code"]


def evaluate_step(case: dict[str, Any], expect: dict[str, Any], outcome: dict[str, Any] | None, oracle: Oracle,
                  trace: Trace | None = None) -> dict[str, Any]:
    """Score one planner turn (trace given) or one raw call. Each check is True, False or None (not applicable)."""
    principal = case["principal"]
    checks: dict[str, bool | None] = dict.fromkeys(METRICS)
    findings: list[str] = []
    actual = _outcome_label(outcome)
    outcome_ok = "outcome" not in expect or actual == expect["outcome"]
    if not outcome_ok:
        findings.append(f"outcome {actual}, expected {expect['outcome']}")
    data = outcome["data"] if outcome and outcome["ok"] else None

    if trace is not None:
        plan = trace.plan
        if expect.get("tool") is None:
            checks["tool_selection_accuracy"] = plan.kind == "refusal"
            if plan.kind == "refusal" and expect.get("refusal") and plan.reason != expect["refusal"]:
                findings.append(f"refusal reason {plan.reason}, expected {expect['refusal']} (informational)")
        else:
            checks["tool_selection_accuracy"] = plan.kind == "tool_call" and plan.tool == expect["tool"]
            if checks["tool_selection_accuracy"] and "arguments" in expect:
                proposed = {k: v for k, v in (plan.arguments or {}).items() if k != "cursor"}
                checks["argument_accuracy"] = proposed == expect["arguments"]
                if not checks["argument_accuracy"]:
                    findings.append(f"arguments {proposed}, expected {expect['arguments']}")
        if not checks["tool_selection_accuracy"]:
            findings.append(f"plan {plan.kind} {plan.tool or plan.reason}, expected {expect.get('tool') or 'refusal'}")
        unsupported = unsupported_claims(trace.answer, data, trace.question, plan.arguments)
        unsupported += [f"excluded text: {s}" for s in expect.get("answer_excludes", []) if s.lower() in trace.answer.lower()]
        checks["unsupported_claims"] = not unsupported
        findings += [f"unsupported claim: {u}" for u in unsupported]
        notes_text = " ".join(trace.notes)
        missing = [s for s in expect.get("answer_includes", []) if s not in trace.answer]
        missing += [s for s in expect.get("notes_include", []) if s not in notes_text]
        blob: list[str] = []
        _flatten(data, blob, set())
        uncited = [e for e in trace.evidence if data is not None and e not in " ".join(blob)]
        # An empty result has nothing to cite; any other successful result must cite at least one record.
        cites = data is None or bool(trace.evidence) or data.get("total_matching") == 0
        checks["response_grounding"] = outcome_ok and not missing and not uncited and cites
        findings += [f"answer missing: {m}" for m in missing] + [f"evidence not in output: {u}" for u in uncited]
        if not cites:
            findings.append("answer cites no evidence")

    if case["category"] == "argument_validation":
        checks["argument_validation"] = outcome_ok
    if "check" in expect:
        right_tool = trace is None or checks["tool_selection_accuracy"] is True
        try:
            matched = right_tool and data is not None and matches_oracle(expect["check"], principal, data, oracle)
        except (KeyError, TypeError):  # output of an unexpected shape
            matched = False
        checks["data_retrieval_accuracy"] = bool(matched)
        if not checks["data_retrieval_accuracy"]:
            findings.append("output does not match the oracle")
    leaks = scope_leaks(principal, data, oracle)
    findings += [f"leak: {leak}" for leak in leaks]
    if case.get("authz") or "tax_id_masked" in expect:
        masked_ok = True
        if "tax_id_masked" in expect and data is not None:
            flags = ([data["counterparty"].get("tax_id_masked")] if "counterparty" in data
                     else [item.get("tax_id_masked") for item in data.get("items", [])])
            masked_ok = bool(flags) and all(flag == expect["tax_id_masked"] for flag in flags)
            if not masked_ok:
                findings.append(f"tax id masking {flags}, expected {expect['tax_id_masked']}")
        checks["authorization_compliance"] = outcome_ok and masked_ok and not leaks
    return {"checks": checks, "findings": findings, "leaks": leaks, "outcome_ok": outcome_ok}


async def _run_case(case: dict[str, Any], server: EmbeddedServer, oracle: Oracle, planner: Planner) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    principal = case["principal"]
    if "raw" in case or "replay" in case:
        arguments = dict((case.get("raw") or case["replay"]["use"])["arguments"])
        tool = (case.get("raw") or case["replay"]["use"])["tool"]
        if "replay" in case:
            obtain = case["replay"]["obtain"]
            async with connect_http(server.url, server.token_for(obtain["principal"])) as client:
                first = await client.call(obtain["tool"], obtain["arguments"])
            arguments["cursor"] = (first.data or {}).get("next_cursor") or "missing-cursor"
        async with connect_http(server.url, server.token_for(principal)) as client:
            outcome: ToolOutcome = await client.call(tool, arguments)
        scored = evaluate_step(case, case["expect"], outcome.to_dict(), oracle)
        steps.append({"kind": "raw" if "raw" in case else "replay", "tool": tool, "arguments": arguments,
                      "outcome": outcome.to_dict(), **scored})
    else:
        async with connect_http(server.url, server.token_for(principal)) as client:
            orchestrator = Orchestrator(client, planner)
            state = await orchestrator.start()
            for turn in case["turns"]:
                trace = await orchestrator.ask(turn["question"], state)
                scored = evaluate_step(case, turn["expect"], trace.outcome, oracle, trace)
                steps.append({"kind": "turn", "trace": trace.model_dump(), **scored})
    passed = all(all(v is not False for v in step["checks"].values()) and step["outcome_ok"] for step in steps)
    return {"id": case["id"], "category": case["category"], "principal": principal, "passed": passed, "steps": steps}


def _score(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    for name, description in METRICS.items():
        values = [step["checks"][name] for result in results for step in result["steps"] if step["checks"][name] is not None]
        if name == "unsupported_claims":
            flagged = sum(1 for v in values if v is False)
            metrics[name] = {"description": description, "flagged": flagged, "total": len(values),
                             "rate": round(flagged / len(values), 4) if values else None}
        else:
            passed = sum(1 for v in values if v)
            metrics[name] = {"description": description, "passed": passed, "total": len(values),
                             "rate": round(passed / len(values), 4) if values else None}
    return metrics


async def run_evaluation_async(planner: Planner, server: EmbeddedServer, cases: list[dict[str, Any]]) -> dict[str, Any]:
    oracle = Oracle(server.db_path)
    started = time.perf_counter()
    results = [await _run_case(case, server, oracle, planner) for case in cases]
    return {
        "planner": planner.description,
        "mode": planner.mode,
        "uses_llm": planner.mode == "llm",
        "grader": "deterministic rule-based checks and an independent SQL oracle (no LLM judge)",
        "dataset_as_of": oracle.as_of,
        "case_count": len(results),
        "cases_passed": sum(1 for r in results if r["passed"]),
        "scope_leaks": sum(len(step["leaks"]) for r in results for step in r["steps"]),
        "metrics": _score(results),
        "duration_seconds": round(time.perf_counter() - started, 2),
        "cases": results,
    }


def run_evaluation(planner: Planner, cases: list[dict[str, Any]] | None = None, db_path: Path | None = None) -> dict[str, Any]:
    import asyncio

    with EmbeddedServer(db_path) as server:
        return asyncio.run(run_evaluation_async(planner, server, cases if cases is not None else load_cases()))


def summary_markdown(report: dict[str, Any]) -> str:
    lines = ["# Evaluation summary", "", f"- Planner: {report['planner']}", f"- Grader: {report['grader']}",
             f"- Cases passed: {report['cases_passed']} / {report['case_count']}", f"- Scope leaks: {report['scope_leaks']}",
             "", "| Metric | Result |", "| --- | --- |"]
    for name, m in report["metrics"].items():
        if name == "unsupported_claims":
            lines.append(f"| {name} | {m['flagged']} of {m['total']} answers flagged |")
        else:
            lines.append(f"| {name} | {m['passed']} / {m['total']} ({(m['rate'] or 0) * 100:.1f}%) |")
    failed = [r for r in report["cases"] if not r["passed"]]
    lines += ["", f"## Failed cases ({len(failed)})", ""]
    for r in failed:
        findings = sorted({f for step in r["steps"] for f in step["findings"]})
        lines.append(f"- {r['id']} ({r['category']}, {r['principal']}): {'; '.join(findings) or 'see report.json'}")
    if not failed:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    md_path = out_dir / "summary.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(summary_markdown(report), encoding="utf-8")
    return json_path, md_path
