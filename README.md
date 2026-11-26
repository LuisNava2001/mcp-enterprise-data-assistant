# MCP Enterprise Data Assistant

A Model Context Protocol (MCP) server, client and agent for a fictional company's finance data. Eight typed,
read-only tools run behind bearer-token authentication; row scope, tax-id masking and argument checks are enforced
in the server, and a deterministic harness evaluates 50 questions end to end over real HTTP.

![Offline demo: evaluation metrics, and an invoice outside the caller's scope returned by the server as NOT_FOUND](docs/img/demo-overview.png)

**Stack:** Python 3.11+ · MCP Python SDK 2.3 (Streamable HTTP) · Pydantic v2 · SQLite · PyJWT · pytest · GitHub
Actions. **Try it:** [Quick Start](#7-quick-start) or open `demo/index.html` offline.

> **Demo scope.** Demo Mode uses a deterministic, rule-based planner and no language model; the optional Claude
> planner (LLM Mode) has not been tested against the live API and has no recorded results. Authentication uses a
> local HS256 demo issuer, not an identity provider. All data is synthetic.

## 1. Project Overview

An assistant answers questions such as *"Has invoice AP-100001 been paid?"* or *"Which suppliers do we owe the
most?"* by calling read-only MCP tools over a SQLite dataset for **Cobalt Harbor Components (fictional)**. The
server is built on the official MCP Python SDK (v2) and serves Streamable HTTP; a separate client and orchestrator
plan one tool call per question and compose an answer that cites its evidence.

## 2. Problem Statement

Connecting an AI assistant to business data raises three questions this project answers in code:

- **Who is asking?** Identity must come from a verified credential, never from what the agent or user types.
- **What may they see?** Row-level scope, sensitive-field masking and read-only access must hold even when the
  planner or model is wrong or manipulated.
- **Can we trust the answer?** Every figure in a response should trace back to a tool result, and that property
  should be measured, not assumed.

## 3. Features

- 8 typed, read-only MCP tools with JSON output schemas, result limits and cursor pagination; no arbitrary SQL.
- OAuth 2.1 resource server: the SDK's auth middleware validates a short-lived JWT on every request, and the
  server publishes Protected Resource Metadata (RFC 9728). There is no authorization server; demo tokens are minted
  locally.
- Server-enforced row scope per fictional user, tax-id masking, unknown-argument rejection, controlled errors.
- Official MCP client over Streamable HTTP; orchestrator separate from the server.
- **Demo Mode** (default): deterministic planner, no LLM. **LLM Mode** (optional): Claude picks the tool;
  implemented but not tested against the live API.
- 50-case evaluation over real HTTP with an independent SQL oracle and seven deterministic metrics.
- Static visual demo of recorded traces, CLI, 114 automated tests, GitHub Actions workflow.

## 4. Architecture

```mermaid
flowchart LR
    U([User]) --> CLI[CLI / demo]
    CLI --> ORC
    subgraph Agent["Orchestration (client side)"]
        ORC[Orchestrator] --> PL{Planner}
        PL -->|Demo Mode| DP[Deterministic rules<br/>no LLM]
        PL -.->|LLM Mode, optional| LP[Claude tool selection]
        ORC --> CMP[Composer<br/>grounded answer]
        ORC --> MC[MCP client<br/>official SDK]
    end
    MC -- "Streamable HTTP + Bearer JWT" --> AUTH
    subgraph Server["MCP server (127.0.0.1)"]
        AUTH[Auth middleware<br/>TokenVerifier] --> TOOLS[8 read-only tools]
        TOOLS --> POL[Access policy<br/>scopes, row scope, masking]
        POL --> REPO[Repository<br/>parameterized SQL]
    end
    REPO --> DB[("SQLite<br/>mode=ro, query_only")]
    EV[Evaluation harness] --> ORC
    EV --> OR[("Independent<br/>SQL oracle")]
```

The orchestrator never touches the database; it only sees what the server returns for the caller's token. Details:
[docs/architecture.md](docs/architecture.md).

## 5. MCP Tools

| Tool | Purpose |
| --- | --- |
| `whoami` | Verified identity, effective scopes and data boundaries |
| `list_invoices` | Filtered, paginated AP or AR invoices |
| `get_invoice_details` | One invoice with payments and counterparty; memo flagged as untrusted |
| `get_invoice_payment_status` | Paid, outstanding, last payment, days overdue |
| `search_suppliers` | Supplier directory search; tax ids masked by default |
| `summarize_payables` / `summarize_receivables` | Totals by status, counterparty or business unit |
| `get_overdue_invoices` | Unpaid invoices past due, most overdue first |

Inputs, outputs, error codes and pagination: [docs/tools.md](docs/tools.md).

## 6. Technology Stack

Python 3.11+, MCP Python SDK 2.3.0 (`MCPServer`, `Client`, Streamable HTTP, auth middleware), Pydantic v2,
SQLite, PyJWT, Uvicorn/Starlette (via the SDK), pytest, plain HTML/CSS/JS for the demo, GitHub Actions. Optional:
the Anthropic Python SDK for LLM Mode.

## 7. Quick Start

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -c constraints.txt -e ".[dev]"

# One shot: private server on a free loopback port, one conversation, shutdown
ledger-assistant ask --embedded --subject ava.ap "Has invoice AP-100001 been paid?" "Mark it as paid"

# Long-running server and a client in two terminals (same secret in both)
export LEDGER_TOKEN_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
ledger-assistant serve                # http://127.0.0.1:8765/mcp, generates data/ledger.db if missing
ledger-assistant ask --subject cora.controller "Which suppliers do we owe the most?"

ledger-assistant eval                 # 50 cases over real HTTP -> evals-output/
python -m pytest -q                   # 114 tests
ledger-assistant build-demo           # refresh demo/data.js, then open demo/index.html
```

Fictional users: `ava.ap`, `ben.ar`, `cora.controller`, `dev.supplier`, `fay.supplier` (see
[docs/tools.md](docs/tools.md#row-scope)). `serve --print-tokens` prints a demo token for each.

## 8. Demo Walkthrough

Open `demo/index.html` in a browser (no server needed). It replays traces recorded from a real evaluation run:
question, planner decision, tool and arguments, structured result, evidence, final answer and the evaluation
checks for that case. The header states the mode: **Demo Mode, deterministic planner, no LLM**.

Suggested path: `#L01` payment status → `#Z01` another business unit's invoice returns not found → `#Q01` an
identity claim in the question is ignored → `#R01` an instruction hidden in a memo is withheld by the client's
composer (below) → `#V06` a
`subject` argument is rejected → `#P01` two-turn pagination.

![Record-text injection case](docs/img/demo-record-injection.png)

## 9. Guardrails & Security

Access controls run in the server and hold for any MCP client: token verification (signature, issuer, audience,
expiry), scope intersection, row scope inside every query, `NOT_FOUND` for out-of-scope records, tax-id masking, no
bank fields, unknown-argument rejection, read-only SQLite (`mode=ro` plus `query_only`), bound parameters only,
capped and user-bound cursors, loopback-only binding and DNS-rebinding protection.

Two safeguards run in this repository's client, not in the server. The composer withholds memo text that matches
an instruction-keyword list, and the planner flags identity claims written in the question. The server returns
memos verbatim, marked in `untrusted_fields`; another MCP client receives that raw text and must treat it as data
itself. Either way, record text cannot trigger an action, because every tool is read-only.

This is **demo authentication, not enterprise authentication**: a local HS256 issuer stands in for an identity
provider. The Protected Resource Metadata names that local issuer, but no authorization-server endpoints exist, so
standard OAuth discovery cannot complete; tokens are minted out of band by the CLI. The OAuth 2.1 / OIDC
production design is in [docs/security.md](docs/security.md).

## 10. Evaluation & Testing

Latest local run (2026-11-23, Demo Mode): **50 / 50 cases passed**, Tool Selection 44/44, Argument Accuracy 36/36,
Argument Validation 7/7, Data Retrieval 26/26, Authorization Compliance 15/15, Unsupported Claims 0 of 44 answers,
Response Grounding 44/44, 0 scope leaks. The deterministic planner was written alongside these cases, so its tool
selection score is a regression baseline rather than a generalization claim; the authorization, validation and
grounding metrics are the meaningful ones. Method and caveats: [docs/evaluation.md](docs/evaluation.md).

114 pytest tests cover the dataset, read-only enforcement, token validation, every tool over an in-memory MCP
session, real Streamable HTTP end to end (including raw JSON-RPC calls that bypass the client), the planner,
composer, LLM adapter (fake client), the harness and demo drift: [docs/testing.md](docs/testing.md). The
evaluation scores above come from the deterministic Demo Mode planner, not from a language model.

## 11. Implementation Status

| Status | Items |
| --- | --- |
| **Implemented** | MCP server (SDK v2, Streamable HTTP), 8 tools, token verification as a resource server, access policy, read-only data layer, pagination, MCP client, deterministic planner, composer, orchestrator, evaluation harness, CLI, static demo, tests, GitHub Actions workflow (Ubuntu and Windows, Python 3.11 to 3.13; results in the repository's Actions tab) |
| **Simulated** | The company and all data; the token issuer and user directory (local HS256 secret instead of an identity provider) |
| **Planned** | OAuth 2.1 / OIDC with an external provider, multi-step plans, LLM-as-judge report, audit log and rate limits, MCP resources and prompts |
| **Not Tested** | LLM Mode against the live API (implemented and unit-tested with a fake client only; the Anthropic SDK is not installed by default); third-party MCP clients such as MCP Inspector (only the official Python SDK client and raw JSON-RPC requests were exercised); macOS (not in the CI matrix) |

## 12. Limitations

- One tool call per question; no joins across tools (for example, a supplier name to its invoices).
- The Demo Mode planner is rule-based English pattern matching and will miss phrasings it was not written for.
- Shared-secret tokens, no refresh, no rotation, no audit trail; fine for localhost, not for deployment.
- Results describe a synthetic dataset; they say nothing about production or real-world performance.

## 13. Roadmap

1. OIDC integration with JWKS validation and resource indicators (design in `docs/security.md`).
2. Multi-step planning with a per-conversation tool budget.
3. Recorded LLM Mode evaluation run, reported separately from Demo Mode.
4. Optional LLM-as-judge for answer quality, kept apart from the deterministic checks.
5. Audit log of tool calls (subject, tool, arguments, outcome) and per-subject rate limits.

## 14. AI-Assisted Development Disclosure

I designed and directed this project; it was implemented with substantial help from Claude Code (Anthropic). I
defined the problem, scope, constraints and acceptance criteria, reviewed the design and the results, and decided
what to publish. Most of the code, tests and documentation were generated with the assistant under that direction,
which is why the commits carry a `Co-Authored-By` trailer. Results quoted here come from the automated tests,
evaluations and CI runs in this repository, on synthetic data. They show that the code behaves as described under
those conditions, not that it is ready for production, and nothing this README marks as simulated, planned or not
tested has been validated.
