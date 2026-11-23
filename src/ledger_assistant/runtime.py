"""Run the real HTTP server inside the current process, on a loopback ephemeral port.

Used by the evaluation harness, the demo builder and the end-to-end tests, so all of them exercise the
same Streamable HTTP transport and token verification as `ledger-assistant serve`.
"""

from __future__ import annotations

import secrets
import socket
import tempfile
import threading
import time
from pathlib import Path
from types import TracebackType

import uvicorn

from .data.generate import PRINCIPALS, generate
from .server.app import ServerSettings, build_http_app
from .server.auth import TokenIssuer

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def principal_scopes() -> dict[str, list[str]]:
    """Every scope each fictional principal may hold, as stored in the dataset."""
    return {row[0]: row[3].split() for row in PRINCIPALS}


class EmbeddedServer:
    """`with EmbeddedServer() as server:` gives a running server, a token issuer and the resource URL."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        if db_path is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="ledger-")
            db_path = generate(Path(self._tmp.name) / "ledger.db")
        self.db_path = db_path
        self.settings = ServerSettings(db_path=db_path, port=free_port(), token_secret=secrets.token_urlsafe(48))
        self.issuer = TokenIssuer(self.settings.token_secret, self.settings.base_url, self.settings.resource_url)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return self.settings.resource_url

    def token_for(self, subject: str, scopes: list[str] | None = None, ttl_seconds: int = 900) -> str:
        """A token requesting the principal's full scope set unless `scopes` narrows it."""
        requested = scopes if scopes is not None else principal_scopes().get(subject, ["ledger:access"])
        return self.issuer.issue(subject, requested, ttl_seconds=ttl_seconds)

    def __enter__(self) -> EmbeddedServer:
        app = build_http_app(self.settings)
        config = uvicorn.Config(app, host=self.settings.host, port=self.settings.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="ledger-mcp-server", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("embedded MCP server failed to start")
            time.sleep(0.02)
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._tmp is not None:
            self._tmp.cleanup()
