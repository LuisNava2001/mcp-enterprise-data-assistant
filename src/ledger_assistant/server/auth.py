"""Local, verifiable demo authentication.

Tokens are short-lived JWTs signed with HMAC-SHA256 by a local issuer. The MCP server acts as an
OAuth 2.1 resource server: the SDK's auth middleware calls `LocalTokenVerifier.verify_token` for every
HTTP request and rejects missing or invalid tokens with 401 before any tool runs. This is a stand-in
for a real identity provider, not enterprise authentication (see docs/security.md for the OIDC design).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

from .access import Principal, Scope

ALGORITHM = "HS256"
DEFAULT_TTL_SECONDS = 15 * 60


class TokenIssuer:
    """Mints demo access tokens. Lives outside the server process in a real deployment."""

    def __init__(self, secret: str, issuer: str, audience: str) -> None:
        if len(secret) < 32:
            raise ValueError("token secret must be at least 32 characters")
        self._secret = secret
        self.issuer = issuer
        self.audience = audience

    def issue(self, subject: str, scopes: Iterable[str], ttl_seconds: int = DEFAULT_TTL_SECONDS, now: float | None = None) -> str:
        issued_at = int(now if now is not None else time.time())
        claims = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": subject,
            "scope": " ".join(sorted(set(scopes))),
            "iat": issued_at,
            "exp": issued_at + ttl_seconds,
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(claims, self._secret, algorithm=ALGORITHM)


class LocalTokenVerifier(TokenVerifier):
    """Validates signature, issuer, audience and expiry, then intersects token scopes with the principal's."""

    def __init__(self, secret: str, issuer: str, audience: str, load_principal: Callable[[str], Principal | None]) -> None:
        self._secret = secret
        self._issuer = issuer
        self._audience = audience
        self._load_principal = load_principal

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.PyJWTError:
            return None
        principal = self._load_principal(str(claims["sub"]))
        if principal is None:
            return None
        requested = set(str(claims.get("scope", "")).split())
        effective = sorted(requested & set(principal.scopes))
        if Scope.ACCESS.value not in effective:
            return None
        return AccessToken(
            token=token,
            client_id=principal.subject,
            subject=principal.subject,
            scopes=effective,
            expires_at=int(claims["exp"]),
            resource=self._audience,
            claims={"role": principal.role},
        )
