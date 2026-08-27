# Live n8n compatibility testing

The normal suite uses a deterministic mock transport. This opt-in suite verifies the
same client against a real n8n instance without granting it arbitrary workflow access.
It targets only the synthetic workflow in
[`examples/registry/synthetic_test_workflow.json`](../examples/registry/synthetic_test_workflow.json).

## Prepare the test target

1. Use an isolated n8n instance, currently pinned by the compatibility matrix to
   `2.35.7`.
2. Import `examples/registry/synthetic_test_workflow.json` and activate it.
3. Create an n8n API key scoped to that isolated test instance.
4. Record the imported workflow ID and its production webhook path.

Do not point this suite at a production instance or a workflow with real side effects.

## Run locally

```bash
export N8N_LIVE_BASE_URL=http://127.0.0.1:5678
export N8N_LIVE_API_KEY=...
export N8N_LIVE_WORKFLOW_ID=...
export N8N_LIVE_WEBHOOK_PATH=/webhook/operator-smoke-test
uv run pytest -m live_n8n -v
```

The suite proves instance health, authenticated workflow retrieval, deterministic
definition hashing, webhook dispatch, response-envelope correlation, and exact
execution retrieval. It never creates, edits, activates, or deletes a workflow.

## Run in GitHub Actions

Configure the `live-n8n` GitHub environment with the three non-secret variables above
and the `N8N_LIVE_API_KEY` secret, then manually run **Live n8n compatibility**. Manual
execution is deliberate: a persistent isolated instance is not yet part of this repo.

Add a compatibility-matrix row only after saving the successful workflow run URL as
evidence. If a new n8n version changes the returned definition, do not weaken drift
detection automatically; follow ADR-008's evidence process first.
