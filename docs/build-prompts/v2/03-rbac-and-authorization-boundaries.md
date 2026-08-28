# Stage 03 prompt — RBAC and authorization boundaries

Copy this entire file into a fresh Claude Code session after Stage 02 is merged.

## Mission

Enforce default-deny authorization over tools, workflows, and environments for the four
normative roles: `viewer`, `operator`, `approver`, and `admin`.

## Required work

- Implement one transport-agnostic policy evaluator with typed decisions and reason codes.
  MCP, CLI, approval routes, repositories, metrics, and audit queries must call the same
  policy boundary rather than reproducing role checks.
- Implement organization, environment, workflow/tag, and tool capability grants according
  to Stage 00. Effective permission is the intersection of all applicable scopes; an
  omitted grant never broadens access.
- Filter list queries at the data layer and re-check object reads/actions in the core.
  Unauthorized workflow and operation identifiers must return the same public result as
  nonexistent identifiers to prevent enumeration.
- Apply authorization to every shipped v1 tool and `whoami`. Preserve out-of-band approval:
  an `approver` role may decide through approved human channels, never through an MCP tool.
- Add admin CLI flows for role assignment and revocation with audit records, confirmation
  for broad grants, and safe previews of effective permissions.
- Add authorization decision logging that is useful for operators but does not reveal
  hidden object names to callers.
- Document a practical least-privilege matrix for a small startup, a centralized RevOps
  team, and a Series C marketing/sales operations organization.

## Required proof

Build a table-driven authorization suite covering every role × tool × environment ×
workflow side-effect class, plus property tests proving that adding a restriction cannot
increase access. Test cross-organization guessing, pagination side channels, cursor reuse,
disabled users, mid-session revocation, conflicting grants, case normalization, default
roles, approval self-grants, and attempts to infer hidden workflows through metrics or
different error timing.

## Completion gate

No adapter may contain ad hoc role logic. Add an import/architecture contract if needed.
All existing behavior must pass under the development principal, and every remote action
must carry a resolved identity and policy decision. Return the capability matrix and Stage
04 entry criteria.
