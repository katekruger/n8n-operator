# Stage 00 prompt — baseline and v2 contract closure

Copy this entire file into a fresh Claude Code session. Execute this stage only.

## Mission

Turn the existing Phase 10 outline into an implementation-ready v2 contract without
changing runtime behavior. Start from the latest repository state, treat
`docs/BUILD_PLAN.md` as normative, and reconcile it with every ADR and shipped v1
contract. The result must make the next stages difficult to misunderstand.

## Working rules

1. Fetch and inspect the latest `main`, tags, open pull requests, worktree status, and
   current CI. Never discard unrelated work. Create a focused branch from current main.
2. Read `README.md`, `docs/BUILD_PLAN.md`, `docs/ARCHITECTURE.md`, `docs/THREAT_MODEL.md`,
   `docs/MCP_TOOLS.md`, all ADRs, the storage models, core service, MCP adapter, approval
   adapter, configuration, and contract tests before editing.
3. Verify current dependency APIs from installed packages or primary documentation; do
   not design against remembered library behavior.
4. Preserve every v1 guarantee. If a v2 decision would alter a state, transition,
   invariant, tool count, or security boundary, document the conflict before coding.
5. Do not tag, release, publish, change repository settings, or merge your own PR.

## Deliverables

- Replace the v2 sketch in `docs/MCP_TOOLS.md` with complete contracts for `whoami`,
  `list_environments`, `request_approval`, `get_approval_status`, `retry_operation`,
  `diff_workflow_definition`, `get_metrics`, and `list_audit_events`.
- Specify every v1 tool’s v2 `environment` behavior, default resolution, result field,
  pagination, authorization filtering, and error semantics.
- Extend the normative build plan with v2 data-model additions, new security boundaries,
  acceptance criteria, and an explicit implementation checklist matching stages 01–11.
- Add or update ADRs for unresolved choices: tenant model, OIDC trust and session model,
  authorization evaluation, environment overlays, quorum semantics, notification delivery,
  metrics cardinality/privacy, and alert-hook delivery. Do not invent ADRs for decisions
  already settled by ADR-004 or ADR-012.
- Add three end-to-end user journeys to the architecture documentation:
  - startup GTM engineer operating staging and production;
  - RevOps team requiring two-person approval for a bulk CRM update;
  - marketing operations investigating campaign-sync drift or a failed enrichment run.
- Create a traceability matrix mapping each v2 outcome and tool to acceptance criteria,
  tests, documentation, and its implementing stage.
- Add contract tests that mechanically catch documentation/tool-count/acceptance-criteria
  drift. This stage must not add runtime tools.

## Decisions that must be explicit

- Organization membership and isolation model.
- Human versus service principal semantics.
- Whether one OIDC subject may belong to multiple organizations and how active organization
  selection works.
- Default environment resolution and whether production may ever be implicit.
- Role capability matrix for `viewer`, `operator`, `approver`, and `admin`.
- Workflow-level and environment-level authorization intersection.
- Quorum snapshot behavior when membership changes mid-approval.
- Notification delivery guarantees and deduplication.
- Metric windows, bounded dimensions, and minimum sample behavior for percentiles.
- Audit-query pagination and redaction.

## Required edge cases

Cover cross-organization identifier guessing, removed users, disabled service principals,
OIDC key rotation, clock skew, environment deletion with historical operations, workflow
overlay conflicts, approval self-dealing, duplicate approval decisions, approver removal,
retry of `UNKNOWN`, concurrent retry requests, and metrics that could reveal unauthorized
workflow names.

## Completion gate

Run the entire existing non-live gate and documentation consistency tests. Demonstrate that
the MCP server still exposes exactly twelve v1 tools. Return a handoff containing decisions,
open risks, changed files, test evidence, and the exact entry criteria for Stage 01.
