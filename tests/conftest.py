from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ledger_assistant.data.generate import generate
from ledger_assistant.evaluation.oracle import Oracle
from ledger_assistant.runtime import EmbeddedServer
from ledger_assistant.server.access import Principal
from ledger_assistant.server.repository import Repository


@pytest.fixture(scope="session")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return generate(tmp_path_factory.mktemp("data") / "ledger.db")


@pytest.fixture(scope="session")
def repository(db_path: Path) -> Repository:
    return Repository(db_path)


@pytest.fixture(scope="session")
def oracle(db_path: Path) -> Oracle:
    return Oracle(db_path)


@pytest.fixture(scope="session")
def principal_for(repository: Repository) -> Callable[..., Principal]:
    """Principal as the token verifier would build it: dataset principal, optionally with fewer scopes."""

    def build(subject: str, scopes: set[str] | None = None) -> Principal:
        p = repository.principal(subject)
        assert p is not None
        return Principal(p.subject, p.display_name, p.role, frozenset(scopes) & p.scopes if scopes else p.scopes,
                         p.business_units, p.supplier_id)

    return build


@pytest.fixture(scope="session")
def http_server(db_path: Path) -> Iterator[EmbeddedServer]:
    with EmbeddedServer(db_path) as server:
        yield server
