## What changed

Describe the user-visible or operator-visible outcome.

## Why

Link the issue, acceptance criterion, ADR, threat, or documented limitation this closes.

## Verification

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy`
- [ ] `uv run python scripts/check_docs_consistency.py`
- [ ] `uv run pytest -m "not live_n8n" --cov=src/n8n_operator --cov-fail-under=90`
- [ ] Built-wheel smoke test run when packaging or entry points changed
- [ ] Live-n8n gate run when the n8n API or webhook contract changed

## Security and compatibility

- [ ] No credential, `.env`, database, live registry, or customer data is included
- [ ] Tool schemas and default-deny boundaries remain unchanged, or docs/tests explain why
- [ ] README, changelog, compatibility matrix, and limitations are current

## Rollback

State how to revert this safely and whether a database migration is involved.
