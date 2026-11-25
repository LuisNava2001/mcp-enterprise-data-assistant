# Guardrails and security

This is a local portfolio demo over fictional data. It shows where controls belong in an MCP data service; it is
**not** enterprise authentication and it has not been security-reviewed or penetration-tested.

## Principle

Every access control lives in the server. The planner, the model and the client can be wrong or hostile; the server
still decides who the caller is, what they may read and what leaves the process. Two safeguards run in this
repository's client for answer quality (see [Server and client trust boundary](#server-and-client-trust-boundary));
no access decision depends on them.

## Controls

| Threat | Control | Where | Verified by |
| --- | --- | --- | --- |
| Anonymous access | SDK auth middleware rejects requests without a valid bearer token (HTTP 401 + `WWW-Authenticate`) | `server/app.py`, `server/auth.py` | `tests/test_http.py` |
| Forged or stale tokens | HS256 signature, `iss`, `aud` (the MCP resource URL), `exp`, `iat`, `sub` required; `alg: none` rejected | `LocalTokenVerifier` | `tests/test_auth.py` |
| Scope escalation | Effective scopes = token scopes ∩ principal's allowed scopes; `ledger:access` required | `LocalTokenVerifier`, `token_principal_resolver` | `test_scope_escalation_is_stripped`, `test_narrowed_token_limits_the_session` |
| Identity spoofing via arguments or prompt | No tool takes an identity parameter; unknown arguments are rejected (server); identity claims in the question are ignored and flagged (client planner) | `LedgerMCPServer.call_tool`, `tools.py`, planner | `test_unknown_arguments_are_rejected_at_the_protocol_level` (raw JSON-RPC), `test_identity_comes_from_the_token`, eval cases Q01, Q02, V06 |
| Cross-user data access | Row scope applied inside every SQL query (business units or supplier id); out-of-scope records return `NOT_FOUND` | `access.py`, `repository.py` | eval cases Z01-Z08, `test_access_policy` |
| Sensitive fields | Tax ids masked unless `sensitive:read`; bank accounts are never selected | `access.mask_tax_id`, repository column lists | `test_tax_ids_are_masked_without_sensitive_scope`, `test_bank_accounts_never_leave_the_server` |
| Writes / destructive operations | No write tools; SQLite opened with `mode=ro` plus `PRAGMA query_only = ON`; DB hash unchanged after full sweeps | `repository.py` | `tests/test_readonly.py`, `test_database_is_unchanged_by_a_full_tool_sweep` |
| Arbitrary SQL / injection | No SQL parameter exists; all values are bound parameters; LIKE wildcards escaped; a static test fails if tool input is interpolated into SQL | `repository.py` | `test_server_sql_is_never_built_from_tool_input`, `test_like_wildcards_are_literal`, eval V03, V05 |
| Unbounded results | `limit` 1-50, keyset cursors bound to user, tool, filters and page size | `tools.py`, `pagination.py` | `test_cursor_is_bound_to_subject_tool_and_filters`, eval P03, P04 |
| Prompt injection in records | Server: memo returned verbatim but marked `untrusted_fields: ["memo"]`, server instructions say memos are data, and no tool writes or sends anything, so neither a planner nor a model (LLM Mode) can act on it. Client: this repository's composer withholds memo text matching an instruction-keyword list | `models.py`, `tools.py` (server); `composer.py` (client) | eval R01, R02 |
| Error leakage | Controlled `CODE: message` results; unexpected exceptions are reduced to a generic message by the SDK | `errors.py` | `test_access_policy`, `test_argument_validation` |
| Network exposure | `serve` refuses non-loopback hosts; DNS-rebinding protection from the SDK host check | `cli.py`, SDK | `test_serve_refuses_non_loopback_hosts` |

## Server and client trust boundary

| | Runs in | Holds for any MCP client? |
| --- | --- | --- |
| Token verification, scopes, row scope, `NOT_FOUND`, tax-id masking, no bank fields, unknown-argument rejection, read-only database, bound parameters, cursor binding, loopback binding, DNS-rebinding check | Server | Yes |
| Memo marked as untrusted (`untrusted_fields`, field description, server instructions) | Server | Yes, but it is a label: the text itself is returned |
| Instruction-like memo text withheld from the answer | Client composer (`composer.py`, keyword list) | No. Another client receives the raw memo |
| Identity claims in the question flagged and ignored | Client planner | No, but identity never comes from the question anyway: it comes from the token |

The client-side safeguards are pattern-based and were written alongside the two seeded injection memos that eval
cases R01 and R02 test; they improve answers, they are not security controls.

## Demo authentication (what is simulated)

`TokenIssuer` plays the role of an authorization server: it signs short-lived JWTs with a shared HMAC secret from
`LEDGER_TOKEN_SECRET` (or an ephemeral per-process secret). The MCP server is a proper OAuth 2.1 **resource
server** in shape: the SDK's middleware validates every request through the `TokenVerifier` interface, and the
server publishes Protected Resource Metadata (RFC 9728) at `/.well-known/oauth-protected-resource/mcp`.

What is simulated: the issuer, the user directory (the `principals` table) and the shared secret. There is no
login, consent, refresh token or key rotation.

The metadata's `authorization_servers` lists the local issuer URL, the same value as the tokens' `iss`, because the
SDK requires an issuer for a resource server. Nothing at that URL implements an authorization server: there is no
`/.well-known/oauth-authorization-server`, `/authorize` or `/token` (a test asserts the `404`). An MCP client that
relies on OAuth discovery therefore cannot obtain a token here; demo tokens come from the CLI
(`serve --print-tokens`, or `ask --embedded`, which mints them in-process).

## Production design with OAuth 2.1 / OIDC (planned, not implemented)

1. **Identity provider.** Use an OIDC provider as the authorization server. Clients run the authorization-code flow
   with PKCE; the MCP server never sees passwords.
2. **Resource indicators.** Clients request tokens for this server's resource URL (RFC 8707); the server keeps
   `validate_token_resource=True` so tokens minted for other APIs are rejected.
3. **Asymmetric validation.** Replace HS256 with RS256/ES256 verified against the provider's JWKS (cached, with
   rotation). Check `iss`, `aud`, `exp`, `nbf` and clock skew.
4. **Scopes and groups.** Map provider scopes or group claims to `ap:read`, `ar:read`, `suppliers:read` and
   `sensitive:read`; keep business-unit and supplier entitlements in an authoritative directory, not in the token
   alone, and keep the intersection rule.
5. **Discovery.** Point `issuer_url` at the real provider, so the existing protected-resource metadata advertises a
   working authorization server and MCP clients can discover it and run the flow (the SDK's client-side OAuth
   support). Today it names the local demo issuer, which has no OAuth endpoints.
6. **Operations.** Short token lifetimes, audit logging of tool calls with subject and arguments, rate limits per
   subject, TLS termination in front of the service, secrets from a vault.

## Residual risks and limits

- The shared HMAC secret means anyone holding it can mint tokens; acceptable only for a local demo.
- Demo tokens printed by `serve --print-tokens` are bearer credentials for fictional users; they expire (default 60 minutes).
- No rate limiting, audit log or monitoring.
- The deterministic planner's identity-claim detection and the composer's memo filter are pattern-based client
  code; they are usability aids, not controls. The server-side checks above are the controls. Another MCP client
  receives memo text verbatim.
- LLM Mode sends the question and the verified role/scopes (no records) to the model provider; it is opt-in and
  has not been run against the live API in this repository.
