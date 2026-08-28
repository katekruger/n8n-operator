# Stage 02 prompt — organizations and OIDC identity

Copy this entire file into a fresh Claude Code session after Stage 01 is merged.

## Mission

Introduce trustworthy human and service identity for team operation. Identity must be
resolved once at the transport boundary, carried through the core, and recorded in every
relevant audit event without coupling domain code to a specific identity vendor.

## Required work

- Implement the Stage 00 organization, membership, principal, and service-principal model
  with migrations and repositories on both supported stores.
- Define an identity port and normalized principal context. Implement OIDC discovery,
  issuer/audience/algorithm validation, JWKS caching and rotation, expiry/not-before checks,
  subject mapping, disabled-principal handling, and organization selection.
- Keep stdio usable for local development through an explicit development identity mode.
  It must be visibly non-production and must refuse unsafe non-loopback deployment.
- Add secure bootstrap and admin CLI flows for creating an organization, mapping the first
  administrator, inspecting memberships, disabling a principal, and rotating service
  credentials. Never print secrets after creation.
- Implement the `whoami` contract exactly as specified in Stage 00, including organization,
  principal kind, roles, default environment, and effective-permission summaries. Do not
  expose provider tokens or raw claims.
- Carry actor and organization scope into logs, operations, approvals, and audit records.
  Historical records must remain attributable after a user is renamed or disabled.
- Add provider-neutral setup documentation plus one fully tested reference configuration.

## Required negative tests

Wrong issuer, wrong audience, forbidden algorithm, unsigned token, expired/future token,
unknown `kid`, JWKS rotation, stale cache, token substitution across organizations, disabled
membership, deleted identity-provider account, duplicate subject mapping, missing active
organization, service principal using an interactive-only path, and log/result secret leaks.

## Completion gate

Prove that unauthenticated remote requests fail before reaching the core, that local dev
remains easy, that `whoami` is the thirteenth tool only when v2 mode is enabled, and that
v1 mode remains exactly twelve tools. Run both databases and all transport tests. Return a
threat-model delta and Stage 03 entry criteria.
