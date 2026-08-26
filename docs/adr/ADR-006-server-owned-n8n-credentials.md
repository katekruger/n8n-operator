# ADR-006: Server-owned n8n credentials

- **Status:** Accepted
- **Date:** 2026-08-25
- **Deciders:** Lead architect
- **Phase:** 0 (architecture and bootstrap)
- **Related:** [BUILD_PLAN.md](../BUILD_PLAN.md) section 9.2 (B5, B7), [THREAT_MODEL.md](../THREAT_MODEL.md) T-09, T-22, T-23, T-26

## Context

Operator needs an n8n API key and, per workflow, a webhook authentication secret. Those
credentials are the most valuable assets in the system (A1, A2): the API key is full
control of the instance, and a webhook secret invokes a workflow directly, bypassing
Operator's entire governance layer.

Three places a credential could live:

1. **With the MCP client** — passed as a tool argument or configured in the client.
2. **In the registry file** — alongside the workflow entry that needs it.
3. **With the server** — resolved from the environment or an OS keyring at startup.

Option (1) is common in MCP servers that proxy an upstream API, and it is disqualified
here: the client is Zone A, untrusted by construction, and a credential in an LLM's
context is a credential in every transcript, log, and vector store downstream of it.

Option (2) is where credentials go to be committed to git. The registry is designed to
be version-controlled and reviewed in pull requests, which is exactly why it must never
hold a secret.

## Decision

**Credentials are owned by the server process, resolved indirectly at startup, and never
cross any boundary toward the client or into any artifact.**

1. Credentials are read at startup from environment variables or the OS keyring, held in
   memory, and never written to the registry, the database, or logs.
2. The registry's `trigger.secret_ref` holds an **indirect reference** only —
   `env:NAME` or `keyring:SERVICE/KEY`. A literal value is a load-time error, not a
   warning (rule R6).
3. `trigger.path` is a path component; the instance base URL comes from server config.
   The registry therefore never records where the n8n instance lives (rule R8).
4. No MCP tool accepts a credential in any argument, in any version.
5. No tool result contains a credential, token, n8n workflow ID, or instance URL —
   enforced by an allowlist projection over every response, not by filtering (B5).
6. Structured logging scrubs configured secret values before emission; there is no
   secret column in any table (B7).
7. TLS verification to n8n is not disableable by configuration (T-26).

Verified by AC-18: a Hypothesis property asserts that no configured secret value,
instance URL, or n8n ID appears in any serialized tool result across generated scenarios.

## Consequences

### Positive

- A compromised or manipulated client gains no credential. The most valuable assets
  never enter Zone A at all — not encrypted, not scoped, not at all.
- The registry is safe to commit and review publicly. Every field in it — titles,
  schemas, hashes, risk classes — is reviewable without leaking anything (T-22).
- Model context stays clean. Nothing sensitive lands in transcripts or client-side logs.
- Credential rotation is a server restart, not a client-fleet update.
- The allowlist projection means new internal fields are invisible by default, so
  leakage requires an explicit mistake rather than an omission (T-09).
- The same registry works across environments, since it records no host — which is what
  makes v2's multi-environment overlays straightforward.

### Negative

- The client cannot use its own n8n identity, so in v1 every action is attributed to one
  server-side credential. Attribution comes from the audit log rather than from n8n's own
  logs, and n8n sees a single actor.
- Operator becomes a credential-holding process whose compromise is severe — accepted,
  and the reason Zone B has boundary controls of its own.
- Secret indirection adds startup failure modes: a missing `env:` reference is a hard
  startup error. This is the correct direction to fail, but it needs clear messages.
- Debugging is slightly harder when errors deliberately omit the host (`INSTANCE_UNREACHABLE`
  rather than a raw connection error naming the instance).

### Neutral

- v2's OAuth/OIDC gives users identity *with Operator*; it does not give them n8n
  credentials. The server still owns the n8n side. Per-user n8n credentials are a v3
  question, and only if enterprise deployments demand n8n-side attribution.
- Keyring support is optional; environment variables are sufficient and are what most
  MCP host configurations supply.

## Alternatives considered

**Client-supplied credentials per call.** Rejected: puts A1 and A2 in Zone A, defeats
the entire threat model, and makes governance advisory — a client holding the webhook
secret can bypass Operator entirely.

**Client-supplied credentials at session setup, held server-side.** Slightly better, and
still rejected: the credential passes through the client's configuration and process,
so it lands in host config files and logs. It also removes the operator's ability to
rotate centrally.

**Per-workflow n8n credentials with least privilege.** Genuinely desirable, and limited
by n8n: API keys are not scopable per workflow. Webhook secrets *are* per workflow, and
we use that — `secret_ref` is per entry. Revisit if n8n ships scoped keys.

**Encrypted secrets stored in the registry file** (SOPS/age-style). Rejected for v1:
it adds a key-management problem to solve a problem that indirection already solves, and
an encrypted blob in the registry still invites the habit of putting secrets there.
