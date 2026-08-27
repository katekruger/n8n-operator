# Security Policy

n8n Operator holds an n8n API key and is designed to sit between an LLM and real,
side-effecting actions. Security issues here are taken seriously and reviewed against
the project's own [threat model](docs/THREAT_MODEL.md), not just patched ad hoc.

## Reporting a vulnerability

**Do not open a public GitHub issue for a security vulnerability.**

Report it privately using GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
feature on this repository (**Security** tab → **Report a vulnerability**). If that is
unavailable to you, open a regular issue asking a maintainer to reach out — do not
include exploit details in it.

Include, as applicable:

- What you found and why it matters (which boundary in
  [THREAT_MODEL.md](docs/THREAT_MODEL.md) section 9.2 it crosses, if you can tell).
- Steps to reproduce, or a minimal example.
- The version (git commit or release tag) you tested against.
- Whether you believe it's already listed as a known limitation in
  [V1_LIMITATIONS.md](docs/V1_LIMITATIONS.md) or an accepted/partial threat in
  [THREAT_MODEL.md](docs/THREAT_MODEL.md) — if so, say which one; that's useful signal
  even if it turns out to be a duplicate.

You'll get an acknowledgment as soon as practical. This is a small project without a
dedicated security team or a formal SLA — response time depends on maintainer
availability, not a contract.

## Scope

In scope:

- Anything in `src/n8n_operator/` — the MCP server, the CLI, the approval app, the
  storage layer, the n8n client.
- The documented boundary controls (B1–B13) failing to hold as described.
- A threat marked `mitigated` in [THREAT_MODEL.md](docs/THREAT_MODEL.md) that turns out
  not to be.

Out of scope (already documented, not going to be "found" as new):

- Everything in [THREAT_MODEL.md](docs/THREAT_MODEL.md) section 8 ("Out of scope for
  v1") and [V1_LIMITATIONS.md](docs/V1_LIMITATIONS.md) — read those first. A compromised
  operator machine, n8n's own security, downstream authorization, and unencrypted data
  at rest are all explicitly accepted v1 tradeoffs, not gaps waiting to be reported.
- Vulnerabilities in n8n itself, or in a workflow an operator chose to register — report
  those to the [n8n project](https://github.com/n8n-io/n8n/security) or the workflow's
  owner, respectively.
- Vulnerabilities in a direct dependency with no n8n-Operator-specific exploitation
  path — report those upstream; a dependency bump here is still welcome as a PR.

## Supported versions

Pre-1.0: only the latest tagged release and `main` are supported. There is no
version-support matrix until a stable `1.0` ships (see
[CHANGELOG.md](CHANGELOG.md) for what's shipped so far).

## What "fixed" means here

A fix to a security issue lands as a normal PR, referencing the report (never
including exploit specifics in the public commit message or changelog entry until a
release has shipped), updates the relevant test(s) to prove the fix, and — if it
changes a control's actual behavior — updates
[THREAT_MODEL.md](docs/THREAT_MODEL.md)'s status for the affected threat. A `mitigated`
status is never assigned without a real, tested control backing it (see this
repository's own release-verification discipline in `docs/BUILD_PLAN.md` phase 9).

## Credential handling, for anyone auditing this project

- The n8n API key and any webhook secrets are held in the server process's memory only
  — resolved from environment or OS keychain at startup
  ([ADR-006](docs/adr/ADR-006-server-owned-n8n-credentials.md)) — and never written to
  the registry, the database, or a log line. `logging_setup.py`'s scrubbing filter is
  defense in depth, not the primary control; the primary control is that a credential
  is never constructed into a loggable value in the first place.
- No MCP tool accepts or returns a credential, an n8n instance URL, or a raw n8n
  workflow ID — enforced structurally (the argument/result schemas have no field for
  one), checked by a contract test (`tests/contract/test_mcp_tool_inventory.py`), and
  independently checked by a Hypothesis property test
  (`tests/property/test_redaction_properties.py`).
