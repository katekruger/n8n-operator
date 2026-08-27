# Reconciling `UNKNOWN` operations

`UNKNOWN` is a deliberate, terminal state. Operator reaches it whenever a dispatch to
n8n could not be confirmed one way or the other — a timeout, a lost response, or an
unparseable response body ([ADR-009](adr/ADR-009-dispatch-correlation.md)). No code
path in v1 moves an operation out of `UNKNOWN`, automatically or otherwise
([ADR-005](adr/ADR-005-no-automatic-retry-v1.md)): **a timeout is never inferred to
mean the workflow did not run.** It may have run. It may not have. Deciding which is a
human task, done here, once, against the real evidence.

**Never re-execute an `UNKNOWN` operation's intent by preparing a new one with the same
arguments unless you have first confirmed, by one of the methods below, that the
original dispatch did not take effect.** If it did take effect and you run it again,
you have caused the side effect twice.

## Step 1 — Look at the operation itself

```bash
n8n-operator operations show <operation_id> --json
```

This shows `state: "UNKNOWN"`, the workflow ID, and the arguments that were sent (as
recorded — see [v1 limitations](V1_LIMITATIONS.md#arguments-are-stored-raw-at-rest) for
why these are unredacted here). Note `workflow_id` and `updated_at` (roughly when the
dispatch was attempted) before going further.

## Step 2 — Check whether an execution ID is available

Export the audit record and find this operation's entry:

```bash
n8n-operator audit export --output export.json
python3 -c "
import json
record = json.load(open('export.json'))
op = next(o for o in record['operations'] if o['id'] == '<operation_id>')
print(json.dumps(op['execution_result'], indent=2))
"
```

Two cases:

**`n8n_execution_id` is present.** The workflow's `trigger.correlation:
response_envelope` returned an execution ID before the connection was lost. Open that
specific execution in the n8n UI (**Executions** → search by ID) and read its actual
outcome directly. This is the strong case: you are looking at *the* execution, not a
guess.

**`n8n_execution_id` is `null`.** Either the workflow has `trigger.correlation: none`
(check the registry entry — `n8n-operator registry show <workflow_id>`), or the
response was lost before an ID could be captured. There is nothing exact to match
against. Go to step 3.

## Step 3 — No execution ID: reason from timing and side effects

Without an execution ID, Operator's own records cannot tell you what happened. Use
what n8n and the downstream system *can* tell you:

1. **n8n's execution list**, filtered to the workflow and the approximate time window
   from step 1 (`updated_at` ± the workflow's configured `timeout_seconds`). Look for
   an execution whose input matches the recorded arguments. This is inference, not
   proof — n8n may have received the request and be mid-run, may never have received
   it, or another invocation of the same workflow may coincidentally overlap.
2. **The downstream system directly** — the CRM record, the sent-email log, whatever
   the workflow actually touches. This is usually the most reliable signal: did the
   side effect you expected actually land?
3. **n8n's own logs**, if you have access to them, for a webhook request matching the
   operation's timing.

If you determine the side effect **did not** occur: it's safe to prepare a new
operation with corrected or identical arguments through the normal flow. `UNKNOWN`
itself is not corrected — it stays `UNKNOWN` as an honest record of what Operator
could confirm at the time — but you are no longer blocked from proceeding.

If you determine the side effect **did** occur: nothing further to do. Do not
re-prepare. The `UNKNOWN` record, with your findings, is the audit trail.

If you cannot determine either way: treat it as if it occurred. This is the
conservative direction for anything in `side_effects: external_write` or
`irreversible` — the same reasoning ADR-005 applies to Operator's own behavior applies
to the human closing the loop.

## Recording what you found

`UNKNOWN` is not editable — there is no supported way to change an operation's own
state or attach a note to it after the fact in v1. If you need a durable record of your
findings (recommended for anything above `read_only`), keep it outside Operator: a
ticket, a runbook entry, or a note referencing the operation ID. `audit export`
captures Operator's own side of the story indefinitely; it will not capture your
investigation.

---

## A crash-stranded `EXECUTING` operation

This is a different, rarer case from `UNKNOWN` above: `EXECUTING` never resolved
*at all*, because the Operator process was killed in the narrow window between burning
the handle and the dispatch call completing (T-37; see
[v1 limitations](V1_LIMITATIONS.md#a-crash-stranded-executing-operation-has-no-automated-recovery)).
There is no automatic sweep and no CLI command for this in v1 — resolving it requires a
direct database edit. Treat this as an emergency procedure, not a routine one.

1. **Confirm it's actually stranded, not just slow.** `operations show <operation_id>`
   reporting `EXECUTING` for far longer than the workflow's `timeout_seconds` (registry
   entry) is the signal — a genuinely in-flight dispatch resolves within that window.
2. **Investigate exactly like an `UNKNOWN` operation** (steps 2–3 above): check n8n's
   execution list and the downstream system for whether the dispatch actually landed.
3. **Stop the Operator process** if it's still running against this database (avoid a
   concurrent write while you edit).
4. **Back up the database file** before editing it. This is not optional.
5. **Update the row directly**, choosing the outcome your investigation supports. This
   mirrors exactly what `record_execution_outcome` would have written had the process
   not crashed — using `sqlite3` directly (adjust the path to your `database_url`):

   ```sql
   -- If you confirmed the side effect occurred:
   UPDATE operations
   SET state = 'SUCCEEDED', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
   WHERE id = '<operation_id>' AND state = 'EXECUTING';

   -- If you confirmed it did not occur, or could not determine either way:
   UPDATE operations
   SET state = 'UNKNOWN', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
   WHERE id = '<operation_id>' AND state = 'EXECUTING';
   ```

   Prefer `UNKNOWN` unless you have direct, positive confirmation of `SUCCEEDED` — the
   same conservative-by-default reasoning as step 3 above. Never set it to `FAILED`
   from outside the normal flow; there is no way to attach a real error payload by
   hand, and a bare `FAILED` with no detail is less honest than `UNKNOWN`.

6. **This edit bypasses `core.service` entirely** — it writes no `operation_events`
   row and no hash-chained `audit_log` entry, because those are normally written
   atomically by the code path you're substituting for. `audit verify` will still pass
   (you haven't touched `audit_log`), but the operation's own event history will show a
   gap: the last recorded transition will be `T10` (`EXECUTING`), with no `T13`/`T14`/
   `T15` explaining how it got to its current state. Note the edit — what you changed,
   when, and why — somewhere durable outside Operator, for the same reason noted above.
7. **Restart Operator.** The concurrency slot this operation was occupying is now
   freed (`max_concurrent` no longer counts it, since it left `EXECUTING`).

If this happens more than rarely, it's worth treating as a signal on its own —
investigate why the process is crashing mid-dispatch (resource limits, a supervisor
killing it too aggressively, a genuine bug) rather than only fixing the row each time.
