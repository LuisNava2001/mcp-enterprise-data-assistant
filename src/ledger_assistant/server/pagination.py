"""Opaque keyset cursors bound to the query that produced them.

A cursor carries the last sort key and a fingerprint of the tool, filters and caller. Reusing it with
different filters, another tool or another identity is rejected with INVALID_CURSOR, so a client can
never page through a result set it did not request.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from .errors import ErrorCode, tool_error

MAX_CURSOR_LENGTH = 300


def fingerprint(tool: str, subject: str, filters: dict[str, Any]) -> str:
    material = json.dumps({"tool": tool, "sub": subject, "filters": filters}, sort_keys=True, default=str)
    return hashlib.sha256(material.encode()).hexdigest()[:20]


def encode_cursor(key: list[str], print_: str) -> str:
    raw = json.dumps({"v": 1, "k": key, "f": print_}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None, expected_print: str, key_size: int) -> list[str] | None:
    if cursor is None:
        return None
    try:
        if len(cursor) > MAX_CURSOR_LENGTH:
            raise ValueError("too long")
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        key = data["k"]
        if data.get("v") != 1 or not isinstance(key, list) or len(key) != key_size or not all(isinstance(k, str) for k in key):
            raise ValueError("bad shape")
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise tool_error(ErrorCode.INVALID_CURSOR, "The cursor is malformed. Start again without a cursor.") from exc
    if data.get("f") != expected_print:
        raise tool_error(ErrorCode.INVALID_CURSOR, "The cursor belongs to a different query. Start again without a cursor.")
    return key
