# Evaluation

`ledger-assistant eval` runs 50 synthetic cases end to end: for each case it starts an MCP session over real
Streamable HTTP with a token for the case's fictional user, runs the orchestrator (or a direct tool call), and grades
the result with deterministic checks. No language model is used for grading.

## Cases

Defined in `src/ledger_assistant/evaluation/cases.json`.

| Category | Cases | What it probes |
| --- | --- | --- |
| lookup | 5 | Payment status and invoice details, including a supplier-portal user reading its own invoice |
| not_found | 2 | Nonexistent invoice; supplier search with no match |
| list | 6 | Filters (status, business unit, counterparty, due date), portal default ledger, supplier search |
| overdue | 2 | "more than N days" and "at least N days" argument mapping |
| aggregation | 3 | Summaries by status, business unit and supplier |
| authorization | 8 | Other business unit, other supplier, missing scope, portal aggregates, masking for portal users |
| question_injection | 4 | Identity claims and "ignore your instructions" in the question, bank-account request, raw SQL |
| record_injection | 2 | Instruction-like memos stored in the data (AP-100003, AR-200003) |
| unsupported / insufficient_evidence / identity | 6 | Write requests, forecasts, off-topic, ambiguous ledger, whoami |
| argument_validation | 7 | Over-limit page size, SQL in an id, impossible date, `sql` and `subject` extra arguments, LIKE wildcards, malformed id via the planner |
| pagination | 5 | Two-turn "next page" conversations, cursor replayed by another user, cursor reused with other filters, "next page" with no context |

Direct tool calls (`raw`, `replay`) bypass the planner to simulate a misbehaving or malicious agent.

## Metrics

| Metric | Definition |
| --- | --- |
| Tool Selection Accuracy | Planner chose the expected tool, or refused when a refusal was expected |
| Argument Accuracy | When the tool was right, proposed arguments equal the expected ones (cursor excluded) |
| Argument Validation | In validation cases, invalid or extra arguments returned `INVALID_ARGUMENT` and valid edge input was accepted |
| Data Retrieval Accuracy | Tool output equals an independent SQL oracle (`oracle.py`, written separately from the server code) |
| Authorization Compliance | Expected deny / not-found / masking outcome held and no out-of-scope row, unmasked tax id or bank field appeared |
| Unsupported Claims | Answers containing an invoice/party id, amount, date or day count absent from the tool output and the question, or a forbidden phrase (lower is better) |
| Response Grounding | Answer contains the required facts, cites at least one record when there is one, and every cited id is in the output |

A global **scope-leak** scan also runs over every tool output in every case.

## Latest local result

Run on 2026-11-23 with `ledger-assistant eval` (Demo Mode, Windows, Python 3.11):

| Metric | Result |
| --- | --- |
| Cases passed | 50 / 50 |
| Tool Selection Accuracy | 44 / 44 |
| Argument Accuracy | 36 / 36 |
| Argument Validation | 7 / 7 |
| Data Retrieval Accuracy | 26 / 26 |
| Authorization Compliance | 15 / 15 |
| Unsupported Claims | 0 of 44 answers |
| Response Grounding | 44 / 44 |
| Scope leaks | 0 |

The same numbers are embedded in `demo/data.js`, which is regenerated from a real run.

## How to read these numbers

- **The planner and the cases were written together.** A perfect Tool Selection score for the deterministic
  planner shows the rules and cases agree; it is a regression baseline, not evidence that the planner generalizes
  to unseen phrasing. `tests/test_agent.py` adds paraphrases outside the case set.
- **The safety metrics are the meaningful ones.** Authorization Compliance, Argument Validation, scope leaks and
  Unsupported Claims test server controls and answer grounding that do not depend on phrasing; CI fails on any
  regression in them (`eval` exits non-zero).
- **The harness can fail.** `test_harness_detects_a_bad_planner` runs a planner that always lists payables and
  asserts the scores collapse.
- **LLM Mode has no recorded result.** `ledger-assistant eval --planner llm` is implemented but has not been run;
  no LLM scores are reported anywhere in this repository.
- **LLM-as-judge is not implemented.** If added, it should stay a separate, clearly labelled report next to the
  deterministic checks, never replace them.
