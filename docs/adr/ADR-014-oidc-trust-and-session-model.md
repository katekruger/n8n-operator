# ADR-014: OIDC trust and session model

- **Status:** Accepted
- **Date:** 2026-08-28
- **Deciders:** Lead architect
- **Phase:** 0.1 continuation (v2 stage 00, contract closure), implemented in phase 10 (v2)
- **Related:** [ADR-013](ADR-013-organization-tenant-and-principal-model.md), [ADR-006](ADR-006-server-owned-n8n-credentials.md), [BUILD_PLAN.md](../BUILD_PLAN.md) section 9 (boundary B9), [THREAT_MODEL.md](../THREAT_MODEL.md) T-14, T-33, T-34

## Context

v1's transport security (boundary B9) is a single static bearer token: adequate for "is
this caller allowed to talk to the server at all," useless for "which human or service is
this." v2 needs real identity for RBAC, approval attribution, and audit records to mean
anything (`decided_by` in the `approvals` table has said `local` unconditionally since
phase 1 and now has to say something real).

Left unspecified, OIDC integration invites two specific mistakes: trusting `sub` alone
(which is only unique *within* one issuer — two different IdPs can mint the same `sub`
value, and treating them as the same principal is a real cross-tenant identity
confusion), and re-validating a JWKS key set on every single request (which turns the
IdP's key endpoint into something Operator's own traffic can accidentally hammer, or an
attacker can deliberately hammer by sending tokens with fabricated `kid` values).

## Decision

**Operator is an OAuth 2.1 / OIDC resource server. It validates bearer tokens itself —
no delegation to a reverse proxy — and stdio deployments authenticate as a fixed service
principal rather than gaining a session concept they cannot securely carry.**

### 1. Token validation

Every Streamable HTTP request in v2 carries a bearer JWT (replacing, not layering onto,
v1's static bearer token — the non-loopback bind guard of boundary B9 still applies: a
non-loopback bind still refuses to start without an `Origin` allowlist, [ADR-006]'s TLS
requirement is unchanged, and OIDC is additionally required as soon as more than the
single-organization default exists). Operator validates:

- **Signature**, against the configured issuer's JWKS.
- **`iss`**, exact match against the configured issuer URL. `aud`, exact match against
  Operator's configured client/audience identifier.
- **`exp`/`nbf`/`iat`**, with a **±60 second** clock-skew tolerance, applied identically
  to all three — not a larger tolerance on one and a smaller on another, which would be
  an easy inconsistency to introduce by accident and a real one to exploit.
- **Identity**, as the pair `(iss, sub)` — never `sub` alone (see Context). This pair is
  what `principals.external_issuer`/`external_subject` stores and matches against
  ([ADR-013](ADR-013-organization-tenant-and-principal-model.md)).

An invalid token in any of the above respects is `INVALID_TOKEN`, uniformly — Operator
does not distinguish "bad signature" from "wrong audience" from "expired" in what it
tells the caller, the same anti-oracle discipline ADR-002 already applies to
`WORKFLOW_NOT_FOUND` (a caller probing for which specific check failed learns nothing
useful either way).

### 2. Key rotation

The JWKS document is fetched once and cached by `kid`. A token whose `kid` is not in the
cache triggers **exactly one** re-fetch of the JWKS document, not a fetch per request —
re-fetches for a `kid` still not found after that single refresh are rate-limited (a
fixed minimum interval between re-fetch attempts, regardless of how many unmatched `kid`
values arrive in between). A `kid` that is not found after the rate-limited re-fetch is
`INVALID_TOKEN`. This bounds the cost of both legitimate rotation (one extra fetch,
once, per actual rotation) and a forged-`kid` denial-of-service attempt against the
IdP's own endpoint (rate-limited regardless of request volume).

### 3. Session model: stateless bearer auth, no server-side session store

v2 introduces no session table and no session token distinct from the bearer JWT itself.
Every request is authenticated independently against the token it carries; there is
nothing to invalidate server-side when a user's access should end beyond what
[ADR-013](ADR-013-organization-tenant-and-principal-model.md) already provides
(membership removal, checked on every call — section 4 below) and what the IdP's own
token lifetime provides (a short-lived access token naturally stops working; Operator
does not implement its own revocation list). This keeps the v1 commitment that adapters
are thin and stateless intact — no new "session" concept crosses the core/adapter
boundary ADR-001 defines.

### 4. Removed and disabled principals are re-checked on every call

Neither organization membership nor a service principal's `disabled_at` flag is cached
across requests. Authorization evaluation
([ADR-015](ADR-015-rbac-authorization-evaluation.md)) re-reads both on every tool call
that needs them. A user removed from an organization, or a service principal an admin
disables, loses access on their *next* call — not eventually, not after some cache TTL
expires. The only thing that can still succeed after removal is a token that has not yet
expired reaching the server *before* the removal is queried — the same "correctness does
not depend on a process being up" commitment
([ARCHITECTURE.md](../ARCHITECTURE.md) section 1) applied to authorization freshness
rather than operation expiry.

### 5. stdio has no OIDC session — it authenticates as a configured service principal

A bearer JWT cannot flow over stdio the way it flows over an HTTP `Authorization`
header, and inventing a stdio-specific token-passing convention would be exactly the
kind of protocol-specific special case ADR-001 exists to prevent. **v2 stdio
deployments run as a single, explicitly configured service principal** — the same shape
v1's implicit `local` principal already has, just now a real row with real
organization memberships instead of a hardcoded exemption. A human who needs their own
OIDC identity attributed to operations uses the Streamable HTTP transport. This is a
deliberate, stated boundary, not an oversight: stdio is for a subprocess launched by a
trusted host (Claude Desktop and similar), and that host is the security boundary in
v1 and stays the security boundary for *that transport* in v2 — it does not become an
identity-forwarding channel just because v2 has identities.

## Consequences

### Positive

- No session-fixation or session-hijacking surface exists, because there is no session
  to fixate or hijack — every request stands on its own token.
- `(iss, sub)` as the identity anchor means adding a second IdP (a real scenario for an
  organization migrating identity providers) never silently merges two different
  people who happen to share a `sub` value under different issuers.
- The rate-limited single-refetch rule means key rotation is cheap when it is real and
  bounded when it is an attack, without needing separate code paths for the two cases.
- stdio keeps working exactly as v1 operators already understand it. Nothing about
  Claude Desktop's integration changes when an organization turns on OIDC for its
  Streamable HTTP deployment.

### Negative

- stdio deployments cannot attribute an operation to a specific human in v2 — every
  stdio-originated audit record says the configured service principal, same as v1's
  `local`. An organization that needs per-human attribution for stdio-originated work
  must move that workflow to Streamable HTTP; there is no partial solution.
- Stateless auth means Operator cannot force-revoke an individual still-valid access
  token before its natural expiry (only membership removal, which takes effect on the
  *next* call, not the current in-flight one). Organizations that need immediate
  hard revocation should configure short-lived access tokens at the IdP; Operator does
  not compensate for a long-lived-token IdP configuration.
- The single-refetch-then-rate-limit rule means a legitimate rotation immediately
  followed by a second, unrelated rotation within the rate-limit window sees the second
  one delayed. Accepted: real-world key rotation is not that frequent, and the
  alternative (unlimited re-fetch) is the denial-of-service surface this rule exists to
  close.

## Alternatives considered

**Delegate token validation to a reverse proxy in front of Operator.** Rejected: it
would make Operator's authorization story depend on deployment topology the product
does not control, and a misconfigured or absent proxy would silently degrade to no
authentication at all rather than failing closed. Boundary B9's loopback-or-bearer-token
guard is enforced by Operator itself for the same reason.

**Trust `sub` alone as the identity anchor.** Rejected in Context: two different issuers
can legitimately mint the same `sub` value, and treating them as one principal is a
cross-tenant identity confusion, not a convenience.

**A server-side session store, issued after the first OIDC exchange.** Rejected: it
reintroduces exactly the stateful-adapter concept ADR-001 exists to keep out of the
core/adapter boundary, and buys nothing a short-lived bearer JWT does not already give —
every request already carries everything needed to authenticate it independently.

**Re-fetch the JWKS document on every request, or on every `kid` miss with no rate
limit.** Rejected: both turn the IdP's key endpoint into a target — the first as
background load from Operator's own normal traffic, the second as a denial-of-service
surface an attacker can trigger by sending tokens with fabricated `kid` values.

**Let stdio deployments pass an OIDC token through an environment variable or a
custom initialize-time field.** Rejected: it invents a protocol-specific credential
channel outside the MCP stdio transport's own shape, which is exactly the kind of
special case ADR-001's "adapters are thin" commitment exists to prevent. A configured
service principal is not a workaround; it is what a subprocess launched by a trusted
host actually is.
