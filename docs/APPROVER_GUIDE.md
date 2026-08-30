# Approver guide

For whoever is on the receiving end of a `request_approval` notification, not the
person running `prepare_operation`. Short by design — see
[ADR-010](adr/ADR-010-approval-delivery-and-expiry.md) for the full delivery/expiry
model this summarizes.

## What a notification tells you (and what it deliberately doesn't)

A webhook notification (ADR-018) carries an event type, the operation ID, and a fetch
reference — never the operation's actual arguments, and never enough on its own to
decide anything. It's a "go look" signal, not a decision surface. Treat it as content
you'd be comfortable with landing in a third-party notification tool's logs, because it
might.

## Deciding

```bash
n8n-operator operations approval-status <operation-id>
```

shows the real decision context: workflow ID, risk, side-effect class, **redacted**
arguments (whatever the workflow's own `output.redact` paths hide, stays hidden here
too), drift status (has the live n8n definition changed since this was registered?),
and the approval deadline. This is what to actually read before deciding — not the
notification.

```bash
n8n-operator operations approve <operation-id>
n8n-operator operations approve <operation-id> --yes   # skip the confirmation prompt
```

renders the same context inline and asks for confirmation before recording your
decision. There is no MCP tool that can do this — approving is always a CLI or web-app
action from an authenticated human session (boundary B4); an MCP client can prepare an
operation and ask you to look, but it can never decide for you or as you.

## Self-approval and quorum

You cannot approve an operation you yourself requested — this is enforced by the
approval-policy snapshot taken when the operation entered `PENDING_APPROVAL`, not by a
check that could be skipped later. For a workflow with `limits.quorum_count > 1`
(e.g. `crm.bulk_update_stage` in the [GTM starter kits](GTM_STARTER_KITS.md)),
`approval-status` shows how many of the required distinct approvers have decided so
far; the operation only moves to `APPROVED` once quorum is reached, and a decision
from someone who already decided (or isn't in the eligible-approver snapshot at all)
is rejected rather than silently ignored or double-counted. Membership changes made
after an operation enters `PENDING_APPROVAL` never retroactively change who was
eligible for it — the snapshot taken at request time is what quorum is measured
against, for the life of that one operation.

## The web approval page

A convenience, not a separate authorization path — it renders the identical decision
context and calls the identical approve/reject logic as the CLI, gated by the same
per-approver, single-use, TTL-bounded token either surface would need. Possessing an
approval link is not authority to use it past its expiry or a second time; a stale or
already-used link fails loudly rather than silently doing nothing.

## Related

- [OPERATOR_GUIDE.md](OPERATOR_GUIDE.md) — the other side of this: preparing and
  routing an operation for approval.
- [GTM_STARTER_KITS.md](GTM_STARTER_KITS.md#journey-2--a-revops-team-requiring-two-person-approval-for-a-bulk-crm-update) —
  a full worked two-approver quorum walkthrough.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — "operation stuck in `PENDING_APPROVAL`"
  and related decision-tree branches.
