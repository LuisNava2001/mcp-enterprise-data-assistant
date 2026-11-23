"""LLM Mode adapter with a fake client. No network calls, no credentials, no SDK required."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from ledger_assistant.agent.llm import DEFAULT_MODEL, FALLBACK_BETA, AnthropicPlanner, PlannerUnavailable
from ledger_assistant.agent.planner import PlanningContext, Refusal, ToolCall
from ledger_assistant.client.session import ToolInfo

from .helpers import run

TOOLS = [ToolInfo("get_invoice_payment_status", "Payment status of one invoice.",
                  {"type": "object", "properties": {"invoice_id": {"type": "string"}}, "required": ["invoice_id"],
                   "additionalProperties": False}, None, True)]
IDENTITY = {"subject": "ava.ap", "role": "AP_ANALYST", "scopes": ["ledger:access", "ap:read"], "business_units": ["BU-EAST"]}


class FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response, self.error, self.calls = response, error, []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def fake_client(messages: FakeMessages) -> Any:
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


def response(stop_reason: str, *blocks: Any) -> Any:
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks))


def plan_with(messages: FakeMessages) -> Any:
    planner = AnthropicPlanner(client=fake_client(messages))
    return planner, run(planner.plan("Has AP-100001 been paid?", PlanningContext(tools=TOOLS, identity=IDENTITY)))


def test_tool_use_block_becomes_a_tool_call() -> None:
    block = SimpleNamespace(type="tool_use", name="get_invoice_payment_status", input={"invoice_id": "AP-100001"}, id="t1")
    messages = FakeMessages(response("tool_use", SimpleNamespace(type="thinking", thinking=""), block))
    planner, result = plan_with(messages)
    assert result == ToolCall(tool="get_invoice_payment_status", arguments={"invoice_id": "AP-100001"},
                              rationale=f"Selected by {DEFAULT_MODEL}.")
    assert planner.mode == "llm"


def test_request_shape() -> None:
    messages = FakeMessages(response("end_turn", SimpleNamespace(type="text", text="Cannot help.")))
    plan_with(messages)
    sent = messages.calls[0]
    assert sent["model"] == DEFAULT_MODEL
    assert sent["tools"] == [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in TOOLS]
    assert sent["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert sent["betas"] == [FALLBACK_BETA] and sent["fallbacks"] == "default"
    assert sent["output_config"] == {"effort": "low"}
    assert "thinking" not in sent and "temperature" not in sent
    content = sent["messages"][0]["content"]
    assert "Has AP-100001 been paid?" in content and "AP_ANALYST" in content


def test_text_reply_is_labelled_as_model_output() -> None:
    _, result = plan_with(FakeMessages(response("end_turn", SimpleNamespace(type="text", text="I can only read data."))))
    assert isinstance(result, Refusal) and result.reason == "model_declined"
    assert result.message.startswith("Model reply (LLM-generated, not from tool data)")


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_stop_reasons_without_a_tool(stop_reason: str) -> None:
    _, result = plan_with(FakeMessages(response(stop_reason)))
    assert isinstance(result, Refusal)


def test_api_errors_become_planner_errors() -> None:
    _, result = plan_with(FakeMessages(error=RuntimeError("boom")))
    assert isinstance(result, Refusal) and result.reason == "planner_error"
    assert "boom" not in result.message  # error text is summarised, never echoed


def test_missing_sdk_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(PlannerUnavailable, match=r"pip install -e \.\[llm\]"):
        AnthropicPlanner()
