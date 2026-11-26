# Testing

```bash
pip install -c constraints.txt -e ".[dev]"
python -m pytest -q                      # 114 tests
python -m ledger_assistant eval          # exits non-zero on any safety regression
python -m ledger_assistant build-demo --check
```

## Inventory

| File | Tests | Covers |
| --- | --- | --- |
| `test_dataset.py` | 3 | Reproducible generation (byte-identical dumps), shape, anchor records, fictional identifiers |
| `test_readonly.py` | 4 | Writes fail on repository connections and on `mode=ro` alone; static check that no tool input is interpolated into SQL |
| `test_auth.py` | 12 | Valid token, scope escalation stripped, expired / wrong audience / wrong issuer / bad signature / unknown principal / missing scope / garbage / `alg: none` rejected |
| `test_tools.py` | 35 | Discovery metadata, payment values, access policy matrix, 15 argument-validation cases, masking, bank fields never returned, untrusted memo flag, full pagination walks against the oracle, cursor binding and tampering, literal LIKE wildcards, summaries against the oracle |
| `test_http.py` | 16 | Real Streamable HTTP: 401 with resource metadata pointer, metadata and health endpoints, DNS-rebinding protection, discovery of the eight strict read-only tools, unknown arguments (`sql`, `subject`, `role`, `scopes`) rejected in raw JSON-RPC calls that bypass the SDK client and on every tool through the client, bad tokens cannot open a session, identity from the token, narrowed tokens, database hash unchanged after a full tool sweep |
| `test_agent.py` | 24 | Planner paraphrases outside the eval set, refusals, default ledger from verified identity, identity claims flagged, next-page cursor reuse, composer formatting, instruction-like memo withheld, controlled error answers |
| `test_llm_adapter.py` | 7 | LLM Mode with a fake client: tool_use parsing, request shape (model, tools from MCP schemas, `tool_choice`, fallback beta), labelled model text, refusal and max-token stops, error summarisation, missing SDK |
| `test_evaluation.py` | 7 | Case-set coverage, every metric measured, safety metrics hold, a deliberately bad planner fails, unsupported-claim detector, committed `demo/data.js` equals a fresh run, demo page uses no `innerHTML` or external URLs |
| `test_cli.py` | 6 | `generate-data`, loopback-only `serve`, `token` secret handling, `ask --embedded` conversation, unreachable server |

## Quality gates (local run, 2026-11-23, Windows, Python 3.11)

| Gate | Evidence | Result |
| --- | --- | --- |
| Server starts | `ledger-assistant serve` as a separate process, `/healthz` | passed |
| Real MCP client connects | official `mcp.Client` over Streamable HTTP with a bearer token | passed |
| Tools discovered and executed | `list_tools` returns 8 tools with output schemas; every tool called | passed |
| Results correct | 26 oracle comparisons in the eval plus pagination and summary tests | passed |
| Guardrails | 401, `FORBIDDEN`, `NOT_FOUND`, `INVALID_ARGUMENT`, `INVALID_CURSOR`, masking, forged token refused by the CLI | passed |
| No write access | write attempts fail; database SHA-256 unchanged after tool sweeps and the full eval | passed |
| Demo works | headless Chrome render at 1280 px and 390 px: 50 cases, 7 metric cards, answers and checks present | passed |
| Tests | `python -m pytest -q` | 114 passed |
| Clean copy | suite, evaluation and demo check on a copy holding only the version-controlled files | passed |

## Continuous integration

`.github/workflows/ci.yml` installs the pinned dependencies and runs the test suite, the evaluation (which exits
non-zero on any safety regression) and the demo-drift check on Ubuntu and Windows with Python 3.11 to 3.13.
Results are in the repository's Actions tab; they are not part of the local run above.

Interoperability with third-party MCP clients (for example MCP Inspector) has not been tested; only the official
Python SDK client and raw JSON-RPC requests were used.
