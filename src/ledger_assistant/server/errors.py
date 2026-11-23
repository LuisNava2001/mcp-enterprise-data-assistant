"""Tool errors with stable codes. They reach the client as MCP tool results with is_error=true."""

from __future__ import annotations

from enum import StrEnum

from mcp.server.mcpserver.exceptions import ToolError


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INVALID_CURSOR = "INVALID_CURSOR"


def tool_error(code: ErrorCode, message: str) -> ToolError:
    """Message format "<CODE>: <text>" lets clients branch on the code without parsing prose."""
    return ToolError(f"{code}: {message}")
