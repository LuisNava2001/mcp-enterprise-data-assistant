"""The agent loop for one question: plan -> call one MCP tool -> compose a grounded answer -> trace.

The orchestrator holds no data access of its own. Everything it knows about the caller comes from the server
(`whoami`, authenticated by the bearer token on the MCP connection).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from ..client.session import LedgerClient, ToolInfo, ToolOutcome
from .composer import compose
from .planner import Planner, PlanningContext, Refusal, ToolCall


class PlanRecord(BaseModel):
    kind: Literal["tool_call", "refusal"]
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    rationale: str | None = None
    reason: str | None = None
    identity_claim_ignored: bool = False


class Trace(BaseModel):
    question: str
    subject: str
    mode: Literal["demo", "llm"]
    planner: str
    plan: PlanRecord
    outcome: dict[str, Any] | None
    answer: str
    evidence: list[str]
    notes: list[str]
    duration_ms: float


@dataclass
class ConversationState:
    identity: dict[str, Any]
    tools: list[ToolInfo]
    last_call: ToolCall | None = None
    next_cursor: str | None = None


def plan_record(plan: ToolCall | Refusal) -> PlanRecord:
    if isinstance(plan, ToolCall):
        return PlanRecord(kind="tool_call", tool=plan.tool, arguments=plan.arguments, rationale=plan.rationale,
                          identity_claim_ignored=plan.identity_claim_ignored)
    return PlanRecord(kind="refusal", reason=plan.reason, identity_claim_ignored=plan.identity_claim_ignored)


class Orchestrator:
    def __init__(self, client: LedgerClient, planner: Planner) -> None:
        self.client = client
        self.planner = planner

    async def start(self) -> ConversationState:
        """Discover tools and fetch the verified identity once per conversation."""
        tools = await self.client.list_tools()
        who = await self.client.call("whoami", {})
        if not who.ok or who.data is None:
            raise RuntimeError(f"whoami failed: {who.error_code} {who.message}")
        return ConversationState(identity=who.data, tools=tools)

    async def ask(self, question: str, state: ConversationState) -> Trace:
        started = time.perf_counter()
        context = PlanningContext(tools=state.tools, identity=state.identity, last_call=state.last_call,
                                  next_cursor=state.next_cursor)
        plan = await self.planner.plan(question, context)
        outcome: ToolOutcome | None = None
        if isinstance(plan, ToolCall):
            if plan.tool not in context.tool_names:
                plan = Refusal(reason="planner_error", message=f"The planner proposed an unknown tool: {plan.tool}.",
                               identity_claim_ignored=plan.identity_claim_ignored)
            else:
                outcome = await self.client.call(plan.tool, plan.arguments)
                if outcome.ok and outcome.data is not None and "next_cursor" in outcome.data:
                    base = {key: value for key, value in plan.arguments.items() if key != "cursor"}
                    state.last_call = plan.model_copy(update={"arguments": base})
                    state.next_cursor = outcome.data.get("next_cursor")
        answer = compose(plan, outcome)
        return Trace(
            question=question,
            subject=state.identity["subject"],
            mode=self.planner.mode,
            planner=self.planner.description,
            plan=plan_record(plan),
            outcome=outcome.to_dict() if outcome else None,
            answer=answer.text,
            evidence=answer.evidence,
            notes=answer.notes,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
