# Stage 11 security review — addendum

This addendum records one finding from a later adversarial review pass over Stage 11
that is deliberately left **unfixed** this round, tracked here rather than silently
dropped. It supplements, and does not replace, `docs/evidence/stage11-security-review.md`.

## Finding: `AuditLogRepository.list_page`'s `"workflow"` branch leaks a denied
## caller's principal id and timing across organizations

**Location:** `src/n8n_operator/storage/repository.py`, `AuditLogRepository.list_page`,
the `subject_type == "workflow"` branch (`workflow_clause`).

**What's wrong:** that branch is filtered only by `workflow_id_like_patterns` — it has
no `environment_id`/`organization_id` conjunct, unlike the `subject_type == "operation"`
branch immediately next to it (which correlates against `operations.environment_id` via
a correlated `EXISTS`, precisely to prevent this class of leak — see that branch's own
docstring and T-66 in `docs/THREAT_MODEL.md`).

That absence is *correct* for one part of what this branch returns: workflow
*definitions* are global, not organization-namespaced, so a shared workflow scope
pattern legitimately authorizing "read this shared registry entry's own events" for
every organization that uses it.

It is *not fully correct*, however, because the same branch also carries
`operation.prepare_denied` audit events. `core.service._prepare_operation_impl`
(`src/n8n_operator/core/service.py`, the `except (ArgumentsTooLargeError,
RateLimitedError)` handler, ~line 1818-1842) writes those with:

- `subject_type="workflow"`
- `subject_id=workflow_id`
- `actor=principal_id` — the **denied caller's own principal id**
- `occurred_at` — implicitly, via the row's own timestamp

So: a viewer in Org B holding `workflow_scope="*"` can read, for any workflow id both
Org A and Org B happen to share, *which principal ULIDs in Org A attempted that
workflow and were denied, and when.* That is a genuine cross-tenant identifier +
timing disclosure, through the exact surface T-66 was written to close — T-66's own
text currently asserts the workflow branch is org-agnostic "correct by design," which
is true for workflow-definition rows but not for this class of row.

**Severity relative to T-66:** narrower. T-66 (the operation branch, now fixed) covered
full operation content — arguments, state, execution results — for another
organization's successful and in-flight operations. This finding covers only the
existence, principal id, and timestamp of a *denied* attempt; no operation `detail`
beyond what `operation.prepare_denied` itself carries (already partially redacted —
see T-67, `recent_count`) is exposed. Still a real identifier + timing leak, not a
theoretical one — see the regression test below.

## Why this is left unfixed this round

A correct query-time fix needs to know, at the point `AuditLogRepository.list_page`
runs, whether the *denied event's own actor* (a principal id) shares an organization
with the *caller*. Today `list_page` is given the caller's resolved `environment_id`
and workflow-scope patterns, but no organization id or principal id to correlate
against — `core.service.list_audit_events` (the sole caller, in `src/n8n_operator/core/service.py`)
never threads the caller's own organization membership down to the repository layer for
this branch. Adding that conjunct correctly therefore requires changing the call site
in `core/service.py` (to pass the caller's organization id(s), or its principal id, into
`list_page`), not just `storage/repository.py`.

At the time this finding was triaged, `src/n8n_operator/core/service.py` (along with
`docs/THREAT_MODEL.md`, `docs/evidence/stage11-security-review.md`,
`tests/integration/test_execute_dispatch.py`, and
`tests/integration/test_metrics_audit_repository.py`) was under active, uncommitted
edit by a concurrent session in this same shared working tree, addressing a separate,
unrelated finding (a T-67 entry redacting `recent_count` from `operation.prepare_denied`
audit details). Editing `service.py` concurrently risked colliding with or clobbering
that in-flight work. Rather than force a `service.py` change through a locked file, or
ship a fix that only *looks* like one — e.g. blanket-redacting `actor` on every
`subject_type="workflow"` row regardless of organization, which would also break the
legitimate cross-org visibility of "someone attempted this shared workflow" that
`operation.prepare_denied` is otherwise meant to preserve for admins reading the shared
registry entry's own audit trail — this finding is documented here and proven with a
regression test instead.

## Regression test

`tests/integration/test_audit_workflow_branch_actor_scope.py::test_workflow_branch_denial_actor_not_visible_across_organizations`
reuses the two-organization/shared-workflow-id fixture pattern from
`tests/integration/test_tenant_isolation_matrix.py`, drives Org A's operator into a real
`operation.prepare_denied` (an oversized-argument rejection against a workflow with a
16-byte `max_argument_bytes` limit), and asserts Org B's viewer — reading with a
wildcard workflow scope — never sees Org A's operator's principal id in the resulting
audit page.

It is marked `xfail(strict=True)`: it fails today (proving the leak is real, not
hypothetical), and `strict=True` means the test suite will start failing the moment
someone fixes the underlying query and this test starts unexpectedly passing — a
forcing function to come back and flip the marker off once fixed, rather than a
silently-stale xfail.

## Follow-up required

This finding is **not** fully closed and must not be treated as such. Before it can be
considered resolved:

1. Thread the caller's organization id(s) (or principal id) from
   `core.service.list_audit_events` down into `AuditLogRepository.list_page`.
2. Add a query-time conjunct to the `"workflow"` branch that admits a
   `subject_type="workflow"` row unconditionally *unless* it is shaped like an
   `operation.prepare_denied`-style per-caller event, in which case it must also
   require the row's own `actor` to share an organization with the caller (a
   correlated subquery against `organization_memberships`, in the spirit of the
   `"operation"` branch's existing `EXISTS` against `operations`).
3. Update `docs/THREAT_MODEL.md`'s T-66 entry text to either fold this case in as fixed,
   or explicitly note the distinction between workflow-definition rows (org-agnostic by
   design) and per-caller denial rows (must be org-scoped) if the two ever need
   different documented treatment.
4. Flip `test_audit_workflow_branch_actor_scope.py`'s `xfail(strict=True)` marker off
   once the fix lands, and confirm it passes.
