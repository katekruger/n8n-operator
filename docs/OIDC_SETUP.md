# OIDC identity setup

> Stage 02 ([ADR-013](adr/ADR-013-organization-tenant-and-principal-model.md),
> [ADR-014](adr/ADR-014-oidc-trust-and-session-model.md)). Operator is an OAuth 2.1 /
> OIDC **resource server** — it validates bearer tokens itself, against the issuer's
> real discovery document and JWKS, never behind a reverse proxy that "already handled
> auth." Any standards-compliant OIDC provider works; this document is provider-neutral
> except for section 4, which is one fully tested, concrete reference configuration.

## 1. What Operator needs from a provider

Nothing provider-specific — five standard OAuth2.1/OIDC capabilities:

1. A discovery document at `<issuer>/.well-known/openid-configuration` (or a directly
   configured `jwks_uri`, if discovery is unavailable in your environment).
2. A JWKS endpoint serving the provider's current signing keys, each with a `kid`.
3. Access tokens signed with an asymmetric algorithm: RS256/384/512, ES256/384/512, or
   PS256/384/512. **`none` and every `HS*` (HMAC) algorithm are rejected outright**,
   regardless of what a token's own header claims — this is a fixed allowlist in
   `identity/oidc.py`, not something a configuration value can loosen.
4. An `aud` claim on issued tokens matching the value you configure as
   `oidc_audience` — most providers need an explicit audience mapper or a
   resource/API identifier configured on the client; it is rarely the client ID by
   default. Get this wrong and every token fails audience validation uniformly (the
   same `INVALID_TOKEN` outcome as every other rejection — see section 5).
5. Reasonably accurate clock. Operator tolerates ±60 seconds of skew on `exp`/`nbf`/
   `iat`; a provider or Operator host further out of sync than that will see
   spuriously expired/not-yet-valid tokens.

## 2. Settings

| Setting | Required | Meaning |
|---|---|---|
| `enable_v2` | yes | Gates the entire v2 surface, including OIDC — `false` (default) never validates a bearer token against any provider. |
| `identity_mode` | yes | `"oidc"` to use a real provider. `"dev"` (the default once `enable_v2=true`) uses one fixed, visibly-labeled local development principal instead — see section 3. |
| `oidc_issuer_url` | yes (oidc mode) | Must exactly match the `iss` claim on issued tokens. |
| `oidc_audience` | yes (oidc mode) | Must exactly match the `aud` claim. |
| `oidc_jwks_uri` | no | Override discovery. Only needed if your provider's discovery document is unreachable from Operator but its JWKS endpoint is directly reachable. |
| `oidc_resource_server_url` | yes (oidc mode) | Operator's **own** externally-reachable URL, published in RFC 9728 protected-resource metadata. Not the same thing as `oidc_audience` — one is who Operator trusts (the token's claimed audience), the other is who Operator says it is. |

A non-loopback HTTP bind requires `identity_mode="oidc"` (or the v1
`http_bearer_token` static-token guard) — `config.py`'s startup validator refuses to
start otherwise (T-48 in [THREAT_MODEL.md](THREAT_MODEL.md)). This is deliberate: a
network-reachable Operator with no real per-caller identity is a threat model failure,
not a convenience.

## 3. Local development: `identity_mode=dev`

stdio transport **always** authenticates as one fixed, idempotently-provisioned
service principal, regardless of `identity_mode` — no OIDC session exists over stdio,
by protocol (ADR-014 section 5). A loopback-only HTTP bind may also use
`identity_mode=dev` for the same reason: convenient local iteration without standing
up a real provider.

This principal's `display_name` is set to
`"local development (identity_mode=dev — never for production)"` — deliberately
unmistakable wherever it appears (a `whoami` result, an audit-log actor, a log line),
so no one mistakes a dev-mode session for an authenticated one after the fact.

## 4. Reference configuration: Keycloak

Keycloak was chosen as the one fully tested reference provider: free, self-hostable,
and widely used, so the configuration below is something you can actually stand up
and validate against, not just a description of what a provider "should" look like.

**Local setup:**

```bash
docker compose -f docker/keycloak-test/docker-compose.yml up -d
export N8N_OPERATOR_TEST_KEYCLOAK_URL=http://127.0.0.1:8081
uv run pytest tests/integration/keycloak/ -v
docker compose -f docker/keycloak-test/docker-compose.yml down -v
```

The realm (`docker/keycloak-test/realm-export.json`) is imported declaratively at
container startup (`start-dev --import-realm`) — no imperative admin-API setup script.
It declares, entirely as fixed, disposable, dev-and-CI-only values (never read from a
secret store, never valid outside this harness):

| | Value |
|---|---|
| Image | `quay.io/keycloak/keycloak:26.0`, `start-dev` mode |
| Port | `127.0.0.1:8081` (loopback only) |
| Realm | `n8n-operator-test` |
| Client ID | `n8n-operator` (confidential, direct-access-grants enabled, client secret `n8n-operator-test-harness-secret-do-not-use-in-prod`) |
| Test user | `alice` / `alice-test-password` |

`oidc_issuer_url` for this realm is `http://127.0.0.1:8081/realms/n8n-operator-test`
and `oidc_audience` is `n8n-operator`. The client carries an explicit **audience
protocol mapper** (`oidc-audience-mapper`, `included.client.audience: n8n-operator`)
— without it, Keycloak's default access token does not carry the client ID as `aud` at
all, and every token would fail `oidc_audience` validation uniformly. This is the one
configuration detail most likely to trip up a naive setup against any provider, not
just Keycloak — see section 1 item 4 above.

CI (`.github/workflows/ci.yml`'s `keycloak` job) runs the identical image and the
identical realm import, so local and CI provisioning are the same code path, not two
that could drift apart.

**A real bug this harness found**, that no amount of mocked-transport testing ever
would have: Keycloak's default realm publishes an `RSA-OAEP`/`use: "enc"` **encryption**
key in its JWKS document alongside the `RS256` signing key — entirely standard, and
something every other IdP that supports encrypted ID tokens does too. `identity/oidc.py`'s
JWKS-fetch loop originally caught only `jwt.InvalidKeyError` around
`PyJWK.from_dict()`, but that call raises the sibling exception `jwt.PyJWKError` for a
key it can parse but can't map to a usable algorithm — exactly this encryption key.
One unusable JWKS entry was silently taking down the entire fetch, which meant every
token verification failed, indistinguishable (by ADR-014's own uniform-failure
discipline) from any other rejection. Fixed by widening the catch to `jwt.PyJWTError`
(the exception both classes actually share) — regression-proven at the unit level in
`tests/unit/test_identity_oidc.py::test_a_jwks_document_with_an_unusable_encryption_key_still_verifies`,
which manually reverting the fix confirms fails without it.

## 5. Required negative tests

Every negative case the stage 02 spec names, and where it is proven:

| Case | Proven by | Notes |
|---|---|---|
| Wrong issuer | `tests/unit/test_identity_oidc.py::test_wrong_issuer_is_rejected` | |
| Wrong audience | `tests/unit/test_identity_oidc.py::test_wrong_audience_is_rejected` | |
| Forbidden algorithm | `tests/unit/test_identity_oidc.py::test_forbidden_algorithm_none_is_rejected`, `::test_forbidden_algorithm_hs256_is_rejected` | The HS256 case is an algorithm-confusion attack: the RSA public key replayed as an HMAC secret. Hand-constructed at the byte level since PyJWT's own `encode()` refuses to sign with a PEM key as an HMAC secret. |
| Unsigned token | `tests/unit/test_identity_oidc.py::test_unsigned_token_is_rejected` | |
| Expired / future token | `tests/unit/test_identity_oidc.py::test_expired_token_is_rejected`, `::test_future_token_before_nbf_is_rejected` (and the positive control, `::test_token_within_clock_skew_tolerance_is_accepted`) | |
| Unknown `kid` | `tests/unit/test_identity_oidc.py::test_unknown_kid_triggers_exactly_one_refetch`, `::test_unknown_kid_refetch_is_rate_limited` | Exactly one re-fetch; a `kid` still not found after it is rate-limited, not retried per request (T-43). |
| JWKS rotation | `tests/unit/test_identity_oidc.py::test_jwks_rotation_is_picked_up_after_the_old_key_stops_working` | A `kid` a re-fetch *does* find is never penalized — distinct from the rate-limited miss case above. |
| Stale cache | Covered jointly by the unknown-`kid`-rate-limit and JWKS-rotation tests above: the cache is trusted between fetches (no per-request re-fetch), and a miss triggers exactly one bounded refresh rather than either blind trust or unbounded re-fetching. | No separate "stale cache" test exists because there is no additional behavior beyond these two to exercise. |
| Token substitution across organizations | `tests/integration/test_mcp_whoami_tool.py::test_whoami_reflects_only_database_membership_never_a_claim_the_caller_asserts` | Identity/access is derived solely from the server-resolved `(iss, sub)` → `principal_id`; no tool argument or JWT claim is ever consulted to pick an organization (B15). |
| Disabled membership | `tests/integration/test_operator_token_verifier.py::test_a_disabled_principal_is_rejected_even_with_a_valid_token` | This schema has no membership-level "disabled" state distinct from a **principal**-level one — `organization_memberships` has only `removed_at` (a hard removal, tested separately below), and disablement lives on `principals.disabled_at`. The stage 02 prompt's "disabled membership" and "disabled principal" are the same code path here; see [V2_TRACEABILITY.md](V2_TRACEABILITY.md) if this schema decision needs revisiting. |
| Removed membership | `tests/integration/test_operator_token_verifier.py::test_a_removed_membership_leaves_whoami_empty_but_the_principal_still_authenticates` | Distinct from the above: the *principal* keeps authenticating (identity is still valid), but a fresh `whoami` read no longer shows the removed organization — never a cached prior state. |
| Deleted identity-provider account | Documented, not separately tested — see [THREAT_MODEL.md](THREAT_MODEL.md) section 8 item 5. | Operator has no IdP-side revocation signal; a deleted account is indistinguishable from any other subject that stops presenting tokens until its last-issued token's own `exp`. The actionable mitigation is `identity disable-principal`, run by an admin — the identical code path the "disabled membership" row above already tests. |
| Duplicate subject mapping | `tests/integration/test_operator_token_verifier.py::test_duplicate_subject_mapping_reuses_the_same_principal` | Authenticating twice as the same `(iss, sub)` reuses one principal row, never creates a second (`uq_principals_external_identity`). |
| Missing active organization | `tests/integration/test_mcp_whoami_tool.py::test_whoami_for_a_principal_with_no_active_organization_returns_an_empty_list` | A freshly JIT-provisioned user, granted no membership, authenticates successfully and gets `"organizations": []` — never an error (MCP_TOOLS.md §5.1's own documented contract). The broader `ENVIRONMENT_REQUIRED` disambiguation AC-36 also names has no `environment` argument to resolve against until stage 04 — see [V2_TRACEABILITY.md](V2_TRACEABILITY.md)'s AC-36 row. |
| Service principal using an interactive-only path | `tests/integration/test_operator_token_verifier.py::test_service_principal_authenticates_by_credential_never_by_jwt` | A service principal has no `external_issuer`/`external_subject` at all — no JWT, however validly signed, can resolve to one; it authenticates only by presenting its configured credential directly. |
| Log/result secret leaks | `tests/integration/test_operator_token_verifier.py::test_resolving_a_service_credential_registers_it_for_log_scrubbing`, `tests/integration/test_cli_identity.py::test_create_service_principal_registers_the_resolved_secret_for_log_scrubbing`, `tests/integration/test_mcp_whoami_tool.py::test_whoami_never_leaks_a_provider_token_or_raw_claim` | A resolved service-principal credential is registered for log scrubbing the moment it is read, in both the server's per-request match path and the CLI's validation path — not only on a successful match (T-47). `whoami`'s result independently never carries a provider token, `external_issuer`/`external_subject`, or a raw claim. |
