"""End-to-end over real Streamable HTTP: bearer-token auth, discovery, isolation and read-only behaviour."""

from __future__ import annotations

import httpx2
import pytest

from ledger_assistant.client.session import ConnectionFailed, connect_http
from ledger_assistant.runtime import EmbeddedServer

from .helpers import run, sha256


def test_request_without_token_gets_401_with_resource_metadata(http_server: EmbeddedServer) -> None:
    response = httpx2.post(http_server.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=10)
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer")
    assert "/.well-known/oauth-protected-resource/mcp" in challenge


def test_protected_resource_metadata_and_health(http_server: EmbeddedServer) -> None:
    base = http_server.settings.base_url
    meta = httpx2.get(f"{base}/.well-known/oauth-protected-resource/mcp", timeout=10).json()
    assert meta["resource"] == http_server.url
    assert meta["scopes_supported"] == ["ledger:access"]
    # The metadata names the local demo issuer (the `iss` of the tokens), but no authorization server runs here:
    # tokens are minted out of band, so standard OAuth discovery cannot go further. The docs say exactly this.
    assert [server.rstrip("/") for server in meta["authorization_servers"]] == [base]
    assert httpx2.get(f"{base}/.well-known/oauth-authorization-server", timeout=10).status_code == 404
    assert httpx2.get(f"{base}/healthz", timeout=10).json() == {"status": "ok"}


def test_dns_rebinding_protection(http_server: EmbeddedServer) -> None:
    headers = {"Authorization": f"Bearer {http_server.token_for('ava.ap')}", "Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    foreign_host = httpx2.post(http_server.url, json=body, headers={**headers, "Host": "evil.example"}, timeout=10)
    foreign_origin = httpx2.post(http_server.url, json=body, headers={**headers, "Origin": "http://evil.example"}, timeout=10)
    assert foreign_host.status_code == 421
    assert foreign_origin.status_code == 403


@pytest.mark.parametrize("kind", ["garbage", "expired", "no-access-scope"])
def test_bad_tokens_cannot_open_a_session(http_server: EmbeddedServer, kind: str) -> None:
    token = {
        "garbage": "not-a-token",
        "expired": http_server.issuer.issue("ava.ap", ["ledger:access", "ap:read"], ttl_seconds=-1),
        "no-access-scope": http_server.issuer.issue("ava.ap", ["ap:read"]),
    }[kind]

    async def go() -> None:
        async with connect_http(http_server.url, token) as client:
            await client.list_tools()

    with pytest.raises(ConnectionFailed):
        run(go())


EIGHT_TOOLS = ["whoami", "list_invoices", "get_invoice_details", "get_invoice_payment_status", "search_suppliers",
               "summarize_payables", "summarize_receivables", "get_overdue_invoices"]


def _raw_call(server: EmbeddedServer, name: str, arguments: dict) -> httpx2.Response:
    """A JSON-RPC tools/call posted directly, bypassing the SDK client, so any rejection is the server's."""
    headers = {"Authorization": f"Bearer {server.token_for('ava.ap')}", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-11-25"}
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    return httpx2.post(server.url, json=body, headers=headers, timeout=10)


def test_raw_protocol_call_is_accepted_without_extra_arguments(http_server: EmbeddedServer) -> None:
    response = _raw_call(http_server, "list_invoices", {"ledger": "AP", "limit": 5})
    assert response.status_code == 200
    result = response.json()["result"]
    assert result.get("isError") is not True
    assert result["structuredContent"]["returned"] == 5


@pytest.mark.parametrize("extra", [{"sql": "DROP TABLE invoices"}, {"subject": "cora.controller"},
                                   {"role": "CONTROLLER"}, {"scopes": ["sensitive:read"]}])
def test_unknown_arguments_are_rejected_at_the_protocol_level(http_server: EmbeddedServer, extra: dict) -> None:
    response = _raw_call(http_server, "list_invoices", {"ledger": "AP", **extra})
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert "INVALID_ARGUMENT" in result["content"][0]["text"]
    assert not result.get("structuredContent")


def test_discovery_over_http_lists_eight_strict_read_only_tools(http_server: EmbeddedServer) -> None:
    async def go():
        async with connect_http(http_server.url, http_server.token_for("ava.ap")) as client:
            return await client.list_tools()

    tools = run(go())
    assert [t.name for t in tools] == EIGHT_TOOLS
    assert all(t.read_only and t.output_schema and t.input_schema.get("additionalProperties") is False for t in tools)


def test_every_tool_rejects_unknown_arguments_over_http(http_server: EmbeddedServer) -> None:
    required = {"list_invoices": {"ledger": "AP"}, "get_overdue_invoices": {"ledger": "AP"},
                "get_invoice_details": {"invoice_id": "AP-100001"}, "get_invoice_payment_status": {"invoice_id": "AP-100001"},
                "search_suppliers": {"query": "Metals"}}

    async def go():
        async with connect_http(http_server.url, http_server.token_for("cora.controller")) as client:
            return {name: (await client.call(name, {**required.get(name, {}), "sql": "DELETE FROM invoices"})).error_code
                    for name in EIGHT_TOOLS}

    assert run(go()) == dict.fromkeys(EIGHT_TOOLS, "INVALID_ARGUMENT")


def test_identity_comes_from_the_token(http_server: EmbeddedServer) -> None:
    async def go():
        results = {}
        for subject in ["dev.supplier", "fay.supplier"]:
            async with connect_http(http_server.url, http_server.token_for(subject)) as client:
                who = await client.call("whoami", {})
                own = await client.call("list_invoices", {"ledger": "AP", "limit": 50})
                spoof = await client.call("list_invoices", {"ledger": "AP", "subject": "cora.controller"})
                results[subject] = (who.data["subject"], {i["counterparty_id"] for i in own.data["items"]}, spoof.error_code)
        return results

    results = run(go())
    assert results["dev.supplier"] == ("dev.supplier", {"SUP-0007"}, "INVALID_ARGUMENT")
    assert results["fay.supplier"] == ("fay.supplier", {"SUP-0012"}, "INVALID_ARGUMENT")


def test_narrowed_token_limits_the_session(http_server: EmbeddedServer) -> None:
    token = http_server.token_for("cora.controller", ["ledger:access", "ap:read"])

    async def go():
        async with connect_http(http_server.url, token) as client:
            return (await client.call("whoami", {})).data, await client.call("summarize_receivables", {})

    identity, receivables = run(go())
    assert identity["scopes"] == ["ap:read", "ledger:access"]
    assert receivables.error_code == "FORBIDDEN"


def test_database_is_unchanged_by_a_full_tool_sweep(http_server: EmbeddedServer) -> None:
    before = sha256(http_server.db_path)

    async def go() -> None:
        async with connect_http(http_server.url, http_server.token_for("cora.controller")) as client:
            for tool in await client.list_tools():
                arguments = {"invoice_id": "AP-100001"} if "invoice_id" in tool.input_schema.get("properties", {}) else {}
                if "ledger" in tool.input_schema.get("required", []):
                    arguments["ledger"] = "AP"
                if "query" in tool.input_schema.get("required", []):
                    arguments["query"] = "Metals"
                outcome = await client.call(tool.name, arguments)
                assert outcome.ok, (tool.name, outcome.message)

    run(go())
    assert sha256(http_server.db_path) == before
