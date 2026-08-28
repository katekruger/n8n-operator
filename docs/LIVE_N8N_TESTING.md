# Live n8n compatibility testing

The normal suite uses a deterministic mock transport. This opt-in suite verifies the
same client against a real n8n instance without granting it arbitrary workflow access.
It targets only the synthetic workflow in
[`examples/registry/synthetic_test_workflow.json`](../examples/registry/synthetic_test_workflow.json)
and never creates, edits, activates, or deletes any other workflow.

## Reproducible setup (Docker)

[`docker/live-n8n/docker-compose.yml`](../docker/live-n8n/docker-compose.yml) pins the
exact n8n version [`docs/COMPATIBILITY_MATRIX.md`](COMPATIBILITY_MATRIX.md) currently
verifies, in a project-scoped, loopback-only, disposable instance:

```bash
scripts/live_n8n_up.sh
```

This automates everything n8n exposes a supported, non-UI interface for: it starts the
container, waits for `/healthz`, imports the synthetic workflow via the n8n CLI, and
**reliably activates its webhook trigger** — confirmed by an actual passing live run,
not just an absence of errors. Getting there took real debugging against a live 2.35.7
instance; the short version, in case a future n8n version regresses it:

- n8n builds its webhook routing table once, at process boot. Neither the legacy
  `update:workflow --active=true` nor its replacement `publish:workflow` registers a
  trigger in the *already-running* process — both only write a database row a
  separate, short-lived CLI process reads and writes. A restart *after* activating is
  required every time (`docker restart n8n-operator-live-test` — the script does this
  for you).
- A webhook node also needs an explicit `webhookId` (a UUID) in its JSON. n8n's
  internal webhook route table uses this as the real lookup key, not just the
  declared `path` — a webhook node imported without one never gets registered, no
  matter which activation mechanism runs afterward. This is why
  [`synthetic_test_workflow.json`](../examples/registry/synthetic_test_workflow.json)'s
  webhook node carries a fixed `webhookId`, and why
  `tests/unit/test_live_n8n_harness.py` asserts it stays there.
- Re-importing an already-imported workflow (e.g. on a rerun of this script)
  deactivates it as a side effect — which is why the script re-publishes and restarts
  unconditionally after every import, not only on first run.

**One manual step remains** — n8n has no documented REST or CLI path to create the
first owner account or an API key; both require the web UI:

1. Open `http://127.0.0.1:5678` and complete the one-time owner account setup.
2. **Settings → n8n API → Create an API Key.**
3. Export the four variables the suite reads (`scripts/live_n8n_up.sh` prints this
   block with the workflow ID it already resolved):

   ```bash
   export N8N_LIVE_BASE_URL=http://127.0.0.1:5678
   export N8N_LIVE_API_KEY=...
   export N8N_LIVE_WORKFLOW_ID=...
   export N8N_LIVE_WEBHOOK_PATH=/webhook/operator-smoke-test
   uv run pytest -m live_n8n -v
   ```

Tear down with `scripts/live_n8n_down.sh` when finished — scoped to this project's own
compose file and named volume only (`n8n-operator-live-test`); it never touches another
container, image, or volume on the host.

Prefer a different mechanism (a local install, a different container runtime, an
existing instance)? Nothing above is required — any n8n reachable at a URL you control
works, as long as it is isolated (never point this at production) and has the synthetic
workflow imported and activated.

## What the suite verifies

- Instance health (`GET /healthz`) and authenticated workflow retrieval, asserting an
  **exact** workflow-ID match — not just "some" workflow came back.
- The retrieved workflow is active, and its definition hash is deterministic (hashing
  the same read twice agrees).
- A real webhook dispatch, the expected synthetic result, response-envelope execution
  correlation, and exact execution retrieval by the correlated ID.
- **Drift detection**, both directions, against the real live definition — never by
  editing the workflow (that's still forbidden): a registered hash that matches the
  live definition passes every preflight check including `definition_unchanged`; a
  registered hash that does not match (as if the workflow had been edited since
  registration) is caught, and preflight refuses readiness.
- **Clean, typed failure** — never a hang or a raw traceback — for a wrong API key
  (401 → `ProviderError`), a wrong workflow ID (404 → `WorkflowMissingOnInstanceError`),
  a wrong webhook path (n8n responds 404, dispatch classifies it `kind="error"`, not
  `"indeterminate"` — a response arrived), and an unreachable instance
  (`InstanceUnreachableError`).

The harness itself (the compose file's pinned image/loopback binding/named
volume/healthcheck, and both scripts) has its own non-live, always-run tests in
`tests/unit/test_live_n8n_harness.py` — a broken harness is caught on every ordinary
CI run, not only the next time someone actually stands one up.

## Run in GitHub Actions

Configure the `live-n8n` GitHub environment with the three non-secret variables above
and the `N8N_LIVE_API_KEY` secret, then manually run **Live n8n compatibility**. Manual
execution is deliberate: a persistent isolated instance is not yet part of this repo's
CI infrastructure — the Docker harness above makes standing one up reproducible, not
automatic on every push.

Add a compatibility-matrix row only after saving the successful workflow run URL as
evidence. If a new n8n version changes the returned definition, do not weaken drift
detection automatically; follow ADR-008's evidence process first — re-run the harness
against the new version (bump the tag in `docker-compose.yml`) and record what changed.
