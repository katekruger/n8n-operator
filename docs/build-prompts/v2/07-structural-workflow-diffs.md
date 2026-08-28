# Stage 07 prompt — structural workflow-definition diffs

Copy this entire file into a fresh Claude Code session after Stage 04 is merged. If Stage
06 is already merged, build against it; always use current `main`.

## Mission

Turn an opaque drift hash into a bounded, reviewable explanation while preserving the
conservative canonicalization rules and never exposing credentials or raw instance data.

## Required work

- Implement a transport-agnostic structural diff model over registered/snapshotted and
  live canonical definitions. Reuse the versioned canonicalization pipeline; do not create
  a second semantic interpretation of n8n JSON.
- Implement `diff_workflow_definition` with environment scope and RBAC. Categorize added,
  removed, and changed nodes; connections; trigger settings; credential-binding presence;
  workflow settings; and unknown fields. Unknown fields remain meaningful, not cosmetic.
- Redact credential identifiers, expressions containing secret material, instance URLs,
  raw workflow IDs, pinned production data, and oversized values before persistence or
  return. Return stable JSON Pointer-like paths, bounded summaries, counts, truncation, and
  registered/live hashes.
- Add human-readable CLI output and machine-readable JSON. Link drift findings from
  preflight and approval views without allowing a diff to override a blocking result.
- Provide sanitized fixtures representing common GTM changes: CRM field mapping, campaign
  audience filter, enrichment provider credential binding, webhook response correlation,
  branching, and error handling.

## Required edge cases

Node reordering, renamed nodes, duplicate names, connection order, large expressions,
unknown n8n fields, unsupported versions, malformed live definitions, cosmetic fields,
credential ID changes, pin data, binary values, Unicode, cycles, huge diffs, environment
scope, hidden workflows, and drift that occurs again between diff and execution.

## Completion gate

Property-test determinism, redaction, bounded output, and the rule that every semantic
change affects either the hash or a visible diff category. Validate fixtures against the
pinned live-n8n harness where possible. Return Stage 08 entry criteria.
