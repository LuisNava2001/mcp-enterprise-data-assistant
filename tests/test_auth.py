import time

import pytest

from ledger_assistant.server.auth import LocalTokenVerifier, TokenIssuer
from ledger_assistant.server.repository import Repository

from .helpers import run

SECRET = "s" * 48
ISSUER = "http://127.0.0.1:8765"
AUDIENCE = "http://127.0.0.1:8765/mcp"


@pytest.fixture()
def issuer() -> TokenIssuer:
    return TokenIssuer(SECRET, ISSUER, AUDIENCE)


@pytest.fixture()
def verifier(repository: Repository) -> LocalTokenVerifier:
    return LocalTokenVerifier(SECRET, ISSUER, AUDIENCE, repository.principal)


def test_short_secret_is_rejected() -> None:
    with pytest.raises(ValueError):
        TokenIssuer("too-short", ISSUER, AUDIENCE)


def test_valid_token_yields_principal_scopes(issuer: TokenIssuer, verifier: LocalTokenVerifier) -> None:
    access = run(verifier.verify_token(issuer.issue("ava.ap", ["ledger:access", "ap:read"])))
    assert access is not None
    assert access.subject == "ava.ap"
    assert access.scopes == ["ap:read", "ledger:access"]
    assert access.resource == AUDIENCE


def test_scope_escalation_is_stripped(issuer: TokenIssuer, verifier: LocalTokenVerifier) -> None:
    token = issuer.issue("ava.ap", ["ledger:access", "ap:read", "ar:read", "sensitive:read"])
    access = run(verifier.verify_token(token))
    assert access is not None
    assert "ar:read" not in access.scopes and "sensitive:read" not in access.scopes


@pytest.mark.parametrize(
    "build",
    [
        lambda i: i.issue("ava.ap", ["ledger:access", "ap:read"], ttl_seconds=-5),  # expired
        lambda i: TokenIssuer(SECRET, ISSUER, "http://127.0.0.1:9999/mcp").issue("ava.ap", ["ledger:access"]),  # audience
        lambda i: TokenIssuer(SECRET, "http://evil.example", AUDIENCE).issue("ava.ap", ["ledger:access"]),  # issuer
        lambda i: TokenIssuer("x" * 48, ISSUER, AUDIENCE).issue("ava.ap", ["ledger:access"]),  # signature
        lambda i: i.issue("nobody", ["ledger:access"]),  # unknown principal
        lambda i: i.issue("ava.ap", ["ap:read"]),  # missing ledger:access
        lambda i: "not-a-jwt",
    ],
    ids=["expired", "wrong-audience", "wrong-issuer", "bad-signature", "unknown-principal", "no-access-scope", "garbage"],
)
def test_invalid_tokens_are_rejected(issuer: TokenIssuer, verifier: LocalTokenVerifier, build) -> None:
    assert run(verifier.verify_token(build(issuer))) is None


def test_inactive_principal_is_rejected(issuer: TokenIssuer) -> None:
    verifier = LocalTokenVerifier(SECRET, ISSUER, AUDIENCE, lambda subject: None)
    assert run(verifier.verify_token(issuer.issue("ava.ap", ["ledger:access"]))) is None


def test_none_algorithm_is_rejected(verifier: LocalTokenVerifier) -> None:
    import jwt

    now = int(time.time())
    unsigned = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "cora.controller", "scope": "ledger:access ap:read",
                           "iat": now, "exp": now + 60}, key=None, algorithm="none")
    assert run(verifier.verify_token(unsigned)) is None
