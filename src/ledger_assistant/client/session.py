"""Connect to the ledger MCP server with the official SDK client, discover tools and call them.

Every call returns a `ToolOutcome` instead of raising, so the orchestrator can show controlled errors to the
user. Error codes come from the server's `CODE: message` convention (see server/errors.py).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client

KNOWN_CODES = {"UNAUTHENTICATED", "FORBIDDEN", "NOT_FOUND", "INVALID_ARGUMENT", "INVALID_CURSOR"}
_CODE_PATTERN = re.compile(r"^(?:Error executing tool \S+: )?([A-Z_]+): (.*)$", re.DOTALL)


class ConnectionFailed(RuntimeError):
    """The server could not be reached or rejected the connection (for example, HTTP 401)."""


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    read_only: bool


@dataclass
class ToolOutcome:
    tool: str
    arguments: dict[str, Any]
    ok: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None
    message: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "arguments": self.arguments, "ok": self.ok, "data": self.data,
                "error_code": self.error_code, "message": self.message}


def parse_error(text: str) -> tuple[str, str]:
    """Map an error result's text to (code, message)."""
    text = text.strip()
    match = _CODE_PATTERN.match(text)
    if match and match.group(1) in KNOWN_CODES:
        return match.group(1), match.group(2).strip()
    if "validation error" in text:
        # Rejected by the SDK's argument validation before the tool body ran.
        detail = text.split(": ", 1)[-1]
        return "INVALID_ARGUMENT", detail.splitlines()[0] + " " + " ".join(line.strip() for line in detail.splitlines()[1:3])
    if text.startswith("Unknown tool") or "Unknown tool" in text:
        return "UNKNOWN_TOOL", text
    return "TOOL_ERROR", text


class LedgerClient:
    """Tool discovery and invocation over an open MCP session."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self._tools: list[ToolInfo] | None = None

    async def list_tools(self) -> list[ToolInfo]:
        if self._tools is None:
            result = await self._client.list_tools()
            self._tools = [
                ToolInfo(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=dict(tool.input_schema),
                    output_schema=dict(tool.output_schema) if tool.output_schema else None,
                    read_only=bool(tool.annotations and tool.annotations.read_only_hint),
                )
                for tool in result.tools
            ]
        return self._tools

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            result = await self._client.call_tool(name, arguments)
        except MCPError as exc:
            return ToolOutcome(name, arguments, ok=False, error_code="PROTOCOL_ERROR", message=str(exc))
        if result.is_error:
            text = " ".join(getattr(block, "text", "") for block in result.content)
            code, message = parse_error(text)
            return ToolOutcome(name, arguments, ok=False, error_code=code, message=message)
        return ToolOutcome(name, arguments, ok=True, data=result.structured_content)


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def _describe_connect_failure(exc: BaseException, url: str) -> str:
    for leaf in _leaves(exc):
        if isinstance(leaf, httpx2.HTTPStatusError):
            return f"server rejected the connection with HTTP {leaf.response.status_code}"
        if isinstance(leaf, httpx2.TransportError):
            return f"could not reach {url} ({type(leaf).__name__})"
        if isinstance(leaf, MCPError):
            return f"server refused the MCP session: {leaf} (check the access token)"
    return f"could not open an MCP session with {url}: {exc}"


@asynccontextmanager
async def connect_http(url: str, token: str, *, timeout: float = 30.0) -> AsyncIterator[LedgerClient]:
    """Open a Streamable HTTP session authenticated with a bearer token."""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(timeout, read=60.0)) as http, AsyncExitStack() as stack:
            try:
                client = await stack.enter_async_context(Client(streamable_http_client(url, http_client=http)))
            except Exception as exc:  # connection, HTTP status and handshake failures arrive wrapped in task groups
                raise ConnectionFailed(_describe_connect_failure(exc, url)) from None
            yield LedgerClient(client)
    except BaseExceptionGroup as group:
        # The transport's task group wraps exceptions raised by the caller's code; hand back the original.
        leaves = _leaves(group)
        if len(leaves) == 1 and isinstance(leaves[0], Exception):
            raise leaves[0] from None
        raise


@asynccontextmanager
async def connect_in_memory(server: Any) -> AsyncIterator[LedgerClient]:
    """In-memory session against an MCPServer instance (tests only; the SDK applies no auth here)."""
    async with Client(server) as client:
        yield LedgerClient(client)
