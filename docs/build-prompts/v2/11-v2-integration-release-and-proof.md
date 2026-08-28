# Stage 11 prompt — v2 integration, release, and proof

Copy this entire file into a fresh Claude Code session after Stages 00–10 are merged.

## Mission

Perform the final v2 closure pass. Do not add feature scope. Prove that the complete system
is operational, secure, recoverable, understandable, and honest enough for other GTM
engineers to adopt.

## Required work

- Audit every v2 outcome, tool contract, acceptance criterion, invariant, boundary, ADR,
  migration, and user journey against current code and retained evidence. Mechanize every
  consistency check that can be mechanized.
- Verify exactly twenty MCP tools in v2 and twelve in v1 compatibility mode. Exercise full
  stdio and Streamable HTTP sessions from built artifacts.
- Run an integrated two-organization, three-environment scenario with PostgreSQL, OIDC,
  RBAC, quorum approval, retry, reconciliation, definition diff, metrics, audit queries,
  alert delivery, and both anchor implementations.
- Rehearse SQLite-v1 to PostgreSQL-v2 migration on a realistic dataset, including rollback,
  audit verification, counts, identity mapping, and historical operation readability.
- Commission a security review of authentication, authorization, tenant isolation, SSRF,
  secret handling, approval forgery, webhook delivery, metrics privacy, audit immutability,
  anchor key custody, and supply-chain configuration. Seed negative tests for every finding.
- Run load/concurrency tests sized for a modern startup and Series C operations team.
  Publish assumptions and measured results; do not claim internet scale.
- Run the live-n8n compatibility harness for every claimed version and retain sanitized
  evidence. Run real Claude and hosted OpenAI-compatible client validations if credentials
  and a safe endpoint are available; otherwise keep those claims explicitly pending.
- Review packaging, provenance, dependencies, CodeQL, Gitleaks, Dependabot, branch
  protection, rollback, backup/restore, and incident runbooks.
- Update README, changelog, compatibility matrix, limitations, architecture, threat model,
  examples, and public release checklist to match facts. Remove stale v1 wording only where
  v2 genuinely replaces it; preserve historical documentation.

## Release decision

Produce a findings-first release report with severity, evidence, owner, and disposition.
Classify each open item as release-blocking, explicitly deferred, or accepted residual
risk. Do not create a tag, GitHub release, PyPI publication, or repository-setting change
without immediate explicit owner approval.

## Completion gate

All required CI checks pass from a clean checkout; both database backends pass their
declared modes; package installation and migration are reproducible; there are no open
critical/high security findings; every public claim has retained evidence; and the GTM
starter journey succeeds without privileged repository knowledge. Return the final diff,
run links, evidence index, rollback plan, and a clear go/no-go recommendation.
