"""Command-line entry point: `ledger-assistant <command>` (or `python -m ledger_assistant`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("data/ledger.db")
DEFAULT_URL = "http://127.0.0.1:8765/mcp"
SECRET_ENV = "LEDGER_TOKEN_SECRET"
TOKEN_ENV = "LEDGER_ACCESS_TOKEN"


def _quiet_logs() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for name in ("httpx2", "httpx", "mcp", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.ERROR)


def _planner(kind: str) -> Any:
    from .agent.planner import DeterministicPlanner

    if kind == "demo":
        return DeterministicPlanner()
    from .agent.llm import AnthropicPlanner, PlannerUnavailable

    try:
        return AnthropicPlanner()
    except PlannerUnavailable as exc:
        raise SystemExit(f"LLM Mode unavailable: {exc}") from None


def cmd_generate_data(args: argparse.Namespace) -> int:
    from .data.generate import generate

    path = generate(args.db)
    print(f"Synthetic dataset written to {path}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .data.generate import PRINCIPALS, generate
    from .runtime import LOOPBACK_HOSTS
    from .server.app import ServerSettings, build_http_app
    from .server.auth import TokenIssuer

    if args.host not in LOOPBACK_HOSTS:
        raise SystemExit("This demo server only binds to a loopback address (127.0.0.1, localhost or ::1).")
    if not args.db.exists():
        generate(args.db)
        print(f"Generated synthetic dataset at {args.db}")
    secret = os.environ.get(SECRET_ENV)
    if not secret:
        secret = secrets.token_urlsafe(48)
        print(f"{SECRET_ENV} is not set: using an ephemeral signing secret valid only for this process.")
    settings = ServerSettings(db_path=args.db, host=args.host, port=args.port, token_secret=secret)
    if args.print_tokens:
        issuer = TokenIssuer(secret, settings.base_url, settings.resource_url)
        print("Demo access tokens (synthetic principals, expire in "
              f"{args.token_ttl // 60} minutes):")
        for subject, _name, _role, scopes, *_ in PRINCIPALS:
            print(f"  {subject}: {issuer.issue(subject, scopes.split(), ttl_seconds=args.token_ttl)}")
    print(f"MCP endpoint (Streamable HTTP): {settings.resource_url}", flush=True)
    _quiet_logs()
    uvicorn.run(build_http_app(settings), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    from .runtime import principal_scopes
    from .server.auth import TokenIssuer

    secret = os.environ.get(SECRET_ENV)
    if not secret:
        raise SystemExit(f"Set {SECRET_ENV} to the same value the server uses.")
    base = f"http://{args.host}:{args.port}"
    scopes = args.scopes.split() if args.scopes else principal_scopes().get(args.subject)
    if scopes is None:
        raise SystemExit(f"Unknown subject {args.subject}; pass --scopes explicitly.")
    print(TokenIssuer(secret, base, f"{base}/mcp").issue(args.subject, scopes, ttl_seconds=args.ttl))
    return 0


def _print_trace(trace: Any) -> None:
    print(f"\nQ ({trace.subject}): {trace.question}")
    print(f"Mode: {trace.planner}")
    if trace.plan.kind == "tool_call":
        print(f"Tool: {trace.plan.tool}  arguments: {json.dumps(trace.plan.arguments, sort_keys=True)}")
        outcome = trace.outcome or {}
        print(f"Result: {'ok' if outcome.get('ok') else outcome.get('error_code')}")
    else:
        print(f"No tool call (refusal: {trace.plan.reason})")
    print(f"Answer:\n{trace.answer}")
    for note in trace.notes:
        print(f"Note: {note}")
    if trace.evidence:
        print(f"Evidence: {', '.join(trace.evidence[:12])}{' ...' if len(trace.evidence) > 12 else ''}")


async def _ask(url: str, token: str, questions: list[str], planner: Any, as_json: bool) -> None:
    from .agent.orchestrator import Orchestrator
    from .client.session import connect_http

    async with connect_http(url, token) as client:
        orchestrator = Orchestrator(client, planner)
        state = await orchestrator.start()
        for question in questions:
            trace = await orchestrator.ask(question, state)
            if as_json:
                print(trace.model_dump_json(indent=2))
            else:
                _print_trace(trace)


def cmd_ask(args: argparse.Namespace) -> int:
    from .client.session import ConnectionFailed

    _quiet_logs()
    planner = _planner(args.planner)
    try:
        if args.embedded:
            from .runtime import EmbeddedServer

            with EmbeddedServer() as server:
                asyncio.run(_ask(server.url, server.token_for(args.subject), args.questions, planner, args.json))
            return 0
        token = os.environ.get(TOKEN_ENV)
        if not token:
            from .runtime import principal_scopes
            from .server.auth import TokenIssuer

            secret = os.environ.get(SECRET_ENV)
            if not secret:
                raise SystemExit(f"Set {TOKEN_ENV}, or {SECRET_ENV} to mint a token locally, or use --embedded.")
            base = args.url.rsplit("/mcp", 1)[0]
            token = TokenIssuer(secret, base, args.url).issue(args.subject, principal_scopes().get(args.subject, []))
        asyncio.run(_ask(args.url, token, args.questions, planner, args.json))
    except ConnectionFailed as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        return 2
    return 0


def _safety_ok(report: dict[str, Any]) -> bool:
    metrics = report["metrics"]
    return (report["scope_leaks"] == 0 and metrics["authorization_compliance"]["rate"] == 1.0
            and metrics["unsupported_claims"]["flagged"] == 0)


def cmd_eval(args: argparse.Namespace) -> int:
    from .evaluation.harness import run_evaluation, summary_markdown, write_report

    _quiet_logs()
    report = run_evaluation(_planner(args.planner))
    json_path, md_path = write_report(report, args.out)
    print(summary_markdown(report))
    print(f"Wrote {json_path} and {md_path}")
    if not _safety_ok(report):
        print("Safety checks failed: scope leak, authorization miss or unsupported claim.", file=sys.stderr)
        return 1
    return 0


def cmd_build_demo(args: argparse.Namespace) -> int:
    from .agent.planner import DeterministicPlanner
    from .demo import render_demo_data
    from .evaluation.harness import run_evaluation

    _quiet_logs()
    content = render_demo_data(run_evaluation(DeterministicPlanner()))
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != content:
            print(f"{args.out} is out of date; run `ledger-assistant build-demo`.", file=sys.stderr)
            return 1
        print(f"{args.out} is up to date.")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(content, encoding="utf-8", newline="\n")
    print(f"Wrote {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger-assistant", description="MCP enterprise data assistant (fictional data).")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate-data", help="write the synthetic SQLite dataset")
    gen.add_argument("--db", type=Path, default=DEFAULT_DB)
    gen.set_defaults(func=cmd_generate_data)

    serve = sub.add_parser("serve", help="run the MCP server over Streamable HTTP on loopback")
    serve.add_argument("--db", type=Path, default=DEFAULT_DB)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--print-tokens", action="store_true", help="print a demo token per synthetic principal")
    serve.add_argument("--token-ttl", type=int, default=3600)
    serve.set_defaults(func=cmd_serve)

    token = sub.add_parser("token", help=f"mint a demo token (needs {SECRET_ENV})")
    token.add_argument("--subject", required=True)
    token.add_argument("--scopes", help="space-separated scopes; defaults to the principal's full set")
    token.add_argument("--ttl", type=int, default=900)
    token.add_argument("--host", default="127.0.0.1")
    token.add_argument("--port", type=int, default=8765)
    token.set_defaults(func=cmd_token)

    ask = sub.add_parser("ask", help="ask one or more questions in a single conversation")
    ask.add_argument("questions", nargs="+")
    ask.add_argument("--subject", default="ava.ap", help="principal to mint a token for (embedded or local secret)")
    ask.add_argument("--url", default=DEFAULT_URL)
    ask.add_argument("--embedded", action="store_true", help="start a private in-process server on a free loopback port")
    ask.add_argument("--planner", choices=["demo", "llm"], default="demo")
    ask.add_argument("--json", action="store_true", help="print full traces as JSON")
    ask.set_defaults(func=cmd_ask)

    ev = sub.add_parser("eval", help="run the evaluation cases over real HTTP")
    ev.add_argument("--planner", choices=["demo", "llm"], default="demo")
    ev.add_argument("--out", type=Path, default=Path("evals-output"))
    ev.set_defaults(func=cmd_eval)

    demo = sub.add_parser("build-demo", help="regenerate demo/data.js from a real evaluation run")
    demo.add_argument("--out", type=Path, default=Path("demo/data.js"))
    demo.add_argument("--check", action="store_true", help="fail if the file differs from a fresh run")
    demo.set_defaults(func=cmd_build_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
