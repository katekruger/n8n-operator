# Stage 05 prompt — team approvals and routing

Copy this entire file into a fresh Claude Code session after Stage 04 is merged.

## Mission

Implement policy-driven N-of-M human approval that works for distributed GTM teams while
keeping approval outside the untrusted MCP channel.

## Required work

- Implement immutable approval-policy snapshots, approval groups, assignments, distinct
  approver decisions, quorum evaluation, rejection semantics, expiry, and organization/
  environment scope from Stage 00.
- Preserve the existing state machine unless the normative contract explicitly changed it.
  Multiple approval records collect while the operation remains `PENDING_APPROVAL`; only
  the quorum decision triggers the existing transition.
- Implement `request_approval` as routing only and `get_approval_status` as a scoped,
  redacted read. The agent must never approve, choose a weaker quorum, or add itself as an
  approver.
- Extend CLI and human approval UI for authenticated team decisions. Show workflow,
  environment, risk, exact redacted arguments, requester, reason, quorum, prior decisions,
  deadlines, and drift/preflight evidence.
- Define a notification port and at least one local/development sink plus an authenticated
  generic webhook sink. Delivery must be idempotent, observable, bounded, redacted, and
  retryable only as notification delivery—not as workflow execution.
- Add escalation/reminder policy only if Stage 00 specified it. Reminders cannot extend
  approval or execution deadlines.
- Audit routing attempts, delivery outcomes, decisions, quorum calculation, and denials.

## Critical edge cases

Requester also an approver, same person in multiple groups, duplicate decisions,
approve-then-reject races, quorum reached concurrently, approver removed mid-flight,
membership added after preparation, policy changed after preparation, expired operation,
notification loss/duplication, webhook outage, forged approval token, cross-organization
approval URL, and two-person approval collapsing to one identity through aliases.

## GTM usability proof

Demonstrate policies for: one approval for a staging CRM sync; two distinct approvals for a
production bulk update; marketing plus legal approval for customer-facing campaign launch;
and an irreversible action that cannot be approved by its requester.

## Completion gate

Add concurrency tests on PostgreSQL, end-to-end human-channel tests, notification contract
tests, accessibility checks for the approval page, and exact tool inventory checks. Return
Stage 06 entry criteria.
