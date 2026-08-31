# Contributing

Thanks for considering it. This project holds real credentials and gates real side
effects — the bar for a change here is "does this still hold under the threat model,"
not just "does it pass CI." Read on before opening a PR; it'll save both of us a round
trip.

## Before you write code

**`docs/BUILD_PLAN.md` is normative.** It defines the state machine, the registry
schema, the MCP tool contracts, the security boundaries, and the acceptance criteria.
If your change touches any of those, read the relevant section first — a change that
contradicts BUILD_PLAN.md without updating it is a bug in the change, not in the doc.
`docs/ARCHITECTURE.md` (components and layering) and `docs/THREAT_MODEL.md` (what every
control defends against) are the next two documents worth reading before a
non-trivial change.

For anything larger than a small fix, open an issue first describing what you want to
change and why. This project has a deliberately narrow v1 scope (see BUILD_PLAN.md
section 3) — a well-intentioned feature PR that's actually v2/v3 scope is a wasted
round trip for both of us if it lands before a design conversation.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/katekruger/n8n-operator.git
cd n8n-operator
uv sync --all-extras --dev
```

## The gate every PR must clear

This is exactly what CI runs (`.github/workflows/ci.yml`) — run it locally before
opening a PR, not after:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python scripts/check_docs_consistency.py
uv run pytest -m "not live_n8n"
```

All five must be clean. `mypy` runs in strict mode; there is no incremental-typing
carve-out. `check_docs_consistency.py` enforces that state names, transition IDs, the
tool inventory, the error taxonomy, ADR structure, and the published file tree in
BUILD_PLAN.md section 4 all agree with each other and with the actual repository —
most of what it catches is a doc edit that fell out of sync with a code change, or vice
versa.

Coverage is gated at 90% line coverage across the whole `src/n8n_operator` package
(`.github/workflows/ci.yml`'s `coverage` job, mirrored in `release.yml`;
`pyproject.toml`'s `[tool.coverage.run]` sets no per-module carve-out). `core/` and
`registry/` are where a bug is a security bug (BUILD_PLAN section 10.4) and are worth
the closest attention, but the enforced gate is not scoped to just those two:

```bash
uv run pytest --cov --cov-report=term-missing
```

`live_n8n`-marked tests need a real n8n instance and never run in CI — see
`tests/integration/mock_n8n.py` for the fake transport most n8n-facing tests use
instead, and BUILD_PLAN.md section 10.1 for the full test-layer breakdown.

## What a good PR looks like here

- **Tests are not optional**, including for a "just a docs fix" PR that also touches
  behavior. A change to `core/` or `registry/` without a new or updated test is very
  unlikely to be merged as-is.
- **Match the existing docstring style.** Nearly every non-trivial function and class
  in this codebase has a docstring explaining *why*, not just what — a design decision,
  a boundary it enforces, or a bug it was written to prevent. Skim a neighboring file
  before adding a new one; the convention is deliberate and consistently applied for a
  reason (it's how a large, security-sensitive codebase stays reviewable).
- **A new architectural decision gets an ADR**, not just a paragraph in a PR
  description — see `docs/adr/` for the existing twenty-one and their format. If you're not
  sure whether your change rises to that level, ask in the issue first.
- **A change to a security boundary updates `docs/THREAT_MODEL.md`.** Never mark a
  threat `mitigated` without a real, tested control backing it — this project's own
  phase 9 release audit found and corrected threat-model entries that had drifted from
  what was actually implemented; don't reintroduce that gap.
- **No secrets, ever, in a commit** — not a real API key, not a realistic-looking one
  in a test fixture without an obvious "this is fake" marker in the string itself (see
  existing test fixtures for the convention: `sk-test-...-do-not-leak-...`, `sk-live-
  should-never-appear-...`). Check `git diff` before pushing, not after.

## Reporting a security issue

**Not here.** See [SECURITY.md](SECURITY.md) — do not open a public issue or PR that
demonstrates a vulnerability before it's been privately reported and, ideally, already
has a fix ready to land alongside the disclosure.

## Commit and PR conventions

- Commit messages: explain *why*, not just what changed — the diff already shows what
  changed. Look at `git log` for the house style.
- Keep a PR focused on one change. A PR that mixes an unrelated refactor with a
  behavioral fix is harder to review and harder to revert if something's wrong.
- Reference the BUILD_PLAN.md section, ADR, or acceptance criterion your change
  relates to, if any — it gives a reviewer the same context you're working from.

## License

By contributing, you agree your contribution is licensed under this project's
[Apache-2.0 license](LICENSE).
