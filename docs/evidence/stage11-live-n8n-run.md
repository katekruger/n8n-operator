# Stage 11 Evidence: Live-n8n Harness Real Run

Captured 2026-08-30 on branch `feat/v2-stage-11-integration-release-and-proof`,
commit `54521934fb713226fe757441d5111a735bf2709c`.

## What was run

```bash
bash scripts/live_n8n_up.sh
```

This brought up the isolated, loopback-only n8n instance defined in
[`docker/live-n8n/docker-compose.yml`](../../docker/live-n8n/docker-compose.yml)
(image `docker.n8n.io/n8nio/n8n:2.35.7`, the exact version
[`COMPATIBILITY_MATRIX.md`](../COMPATIBILITY_MATRIX.md) currently claims), waited for
`/healthz`, imported the synthetic workflow
(`examples/registry/synthetic_test_workflow.json`), and published + restarted it to
register the webhook trigger — all exactly as described in
[`LIVE_N8N_TESTING.md`](../LIVE_N8N_TESTING.md). Resolved workflow ID:
`opTestWorkflow0`.

The one manual, UI-only step — n8n's first-owner-account setup and API-key creation,
which has no documented REST/CLI path — was completed via browser automation:
opened `http://127.0.0.1:5678`, filled in the owner-account form (a throwaway local
`@example.com` address, used only inside this disposable container), then
**Settings → n8n API → Create an API Key** with the default "All" scope and default
30-day expiration.

The four variables the suite reads were then exported and the live-marked suite run:

```bash
export N8N_LIVE_BASE_URL=http://127.0.0.1:5678
export N8N_LIVE_API_KEY=<redacted — generated fresh for this disposable instance, discarded on teardown>
export N8N_LIVE_WORKFLOW_ID=opTestWorkflow0
export N8N_LIVE_WEBHOOK_PATH=/webhook/operator-smoke-test
uv run pytest -m live_n8n -v
```

## Result

```
collecting ... collected 8 items

tests/live/test_live_n8n.py::test_live_instance_health_and_workflow_read PASSED [ 12%]
tests/live/test_live_n8n.py::test_live_dispatch_correlation_and_execution_read PASSED [ 25%]
tests/live/test_live_n8n.py::test_live_preflight_reports_no_drift_against_the_real_current_hash PASSED [ 37%]
tests/live/test_live_n8n.py::test_live_preflight_detects_drift_against_a_stale_hash PASSED [ 50%]
tests/live/test_live_n8n.py::test_live_wrong_api_key_fails_cleanly PASSED [ 62%]
tests/live/test_live_n8n.py::test_live_wrong_workflow_id_fails_cleanly PASSED [ 75%]
tests/live/test_live_n8n.py::test_live_wrong_webhook_path_dispatches_as_an_error_not_a_crash PASSED [ 87%]
tests/live/test_live_n8n.py::test_live_instance_unavailable_fails_cleanly PASSED [100%]

============================== 8 passed, 1493 deselected in 5.31s ===============================
```

**8/8 passed.** No failures, no skips.

This confirms, against a real freshly-provisioned instance (not a mock transport):
instance health and authenticated workflow read with exact workflow-ID matching;
webhook dispatch, execution correlation, and exact execution retrieval; drift
detection in both directions (no false positive against the live definition, and a
real catch of a stale registered hash); and clean, typed failures — not hangs or raw
tracebacks — for a wrong API key, a wrong workflow ID, a wrong webhook path, and an
unreachable instance.

## Teardown

```bash
bash scripts/live_n8n_down.sh
```

Confirmed afterward with `docker ps -a` that no `n8n-operator-live-test` container
remains. The named volume was also removed by `docker compose down --volumes`. No
other containers, volumes, or images on the host were touched.

## Residual gap — stated plainly

**Only one n8n version has been validated: 2.35.7.** This is the only version listed
in [`COMPATIBILITY_MATRIX.md`](../COMPATIBILITY_MATRIX.md), and this run does not
change that. Operator's implementation targets only n8n's stable, documented Public
REST API, which is the basis for expecting broader compatibility across nearby
versions — but "expected" is not "verified." No other n8n version has been run
against this harness. Extending coverage requires bumping the pinned tag in
`docker/live-n8n/docker-compose.yml`, re-running this harness, and adding a new
compatibility-matrix row per ADR-008's evidence process — none of that was done as
part of this task. This gap is carried forward to Task 10's final report, not
silently closed by this evidence file.
