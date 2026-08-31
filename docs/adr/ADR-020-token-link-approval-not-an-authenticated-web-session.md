# ADR-020: Keep the single-use token-link approval model instead of an OIDC-authenticated approval web session

- **Status:** Accepted
- **Date:** 2026-08-31
- **Deciders:** Kate Kruger
- **Phase:** post-10 (v2 closure)
- **Related:** [ADR-010](ADR-010-approval-delivery-and-expiry.md), [ADR-014](ADR-014-oidc-trust-and-session-model.md), [THREAT_MODEL.md](../THREAT_MODEL.md) T-08, T-15, T-16, T-34

## Context

[ADR-010](ADR-010-approval-delivery-and-expiry.md) already records the delivery/expiry
model for approvals in depth: a 256-bit, single-use, TTL-bounded token, stored only as
a `sha256` hash, delivered via a notification link, resolved by a loopback-bound
FastAPI app (`src/n8n_operator/approval/`). What ADR-010 does not record is a decision
that came up freshly during a v2 closure pass (Stage 11): whether that token-link model
should be replaced or extended with a real OIDC-authenticated login session for
approvers — the approver signs in against the org's own identity provider, the app
resolves their authenticated principal server-side, and a decision is tied to that
session rather than to possession of a link.

v2 already has OIDC for everything else ([ADR-014](ADR-014-oidc-trust-and-session-model.md)'s
trust model) — CLI callers and MCP callers authenticate via a validated `(iss, sub)`
pair. The approval app is the one surface in v2 that still authorizes a decision by
*possessing a token* rather than by *authenticating as a principal*. A stage-11
closure prompt asked directly whether this asymmetry should be closed by building a
full OIDC-authenticated approval web UI.

## Decision

**Keep the existing single-use token-link model, unchanged, as the only approval
channel.** Asked directly, the decision-maker declined the OIDC-authenticated web app
in full: "no web app, just the token model is fine." This was a deliberate scope
decision, not a default — the alternative was fully scoped (a stage-11 closure prompt
specified authentication and session lifecycle, CSRF/replay/clickjacking/open-redirect/
session-fixation/confused-deputy protections, redaction rules, and a real
device-authorization CLI fallback in detail) and explicitly rejected in favor of what
already exists.

Options considered:

* Keep the existing single-use token-link model, unchanged, as the only approval
  channel (**chosen**)
* Build a new, OIDC-authenticated approval web session (real login, server-resolved
  principal, decision tied to the authenticated session) alongside or instead of the
  token-link model
* Keep the token-link model but layer OIDC authentication on top of it (a token grants
  access to a decision page, but the page also requires an authenticated session
  before rendering)

## Consequences

### Positive

- The existing model is already well-specified (ADR-010), already defends against the
  threats a naive token link would have (T-08, T-15, T-16, T-34 —
  `docs/THREAT_MODEL.md`), and adding a second, parallel authentication surface for
  the same action (approve/reject) would double the attack surface for the single
  most security-sensitive code path in the product without a stated, concrete gap the
  token-link model actually has.
- It keeps `approval/` small (under 400 lines across `app.py`/`routes.py`) and
  loopback-only (boundary B10) — a real OIDC login flow implies a network-reachable
  surface with a session store, which is a materially larger commitment this repo has
  not made anywhere else in v1/v2's approval path.
- This decision is specific to the *interactive web* approval surface — the CLI
  approval path (`n8n-operator operations approve`) already authenticates its caller
  through the CLI's own OIDC/local-principal resolution, independent of this choice.

### Negative

- The asymmetry named in Context remains: every other v2 caller authenticates via
  OIDC, and the approval app is the one place a decision is authorized by link
  possession plus (for a state-changing POST) Host/Origin/CSRF checks, not by a
  validated principal identity. A stolen or forwarded approval link, used before it
  expires or is consumed, still grants a decision to whoever holds it — ADR-010's own
  threat model already accepts this and pairs it with short TTLs and single-use
  tokens, but it is a real, named tradeoff, not a solved problem. Accepted: closing it
  requires the OIDC-authenticated web app this ADR declines to build.
- Nothing here forecloses revisiting this decision later — if a real gap in the
  token-link model is found (not merely "it's asymmetric with everything else"), that
  would be new information this ADR does not have.

## Alternatives considered

See the options list above; both alternatives were explicitly proposed (in detail, by
name, with a full threat-model requirements list) and explicitly declined by the
repository owner in favor of the status quo.
