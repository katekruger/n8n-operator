# Stage 11 — Packaging, provenance, and CI audit

Audit date: 2026-08-30. Read-only review of `.github/workflows/*.yml`,
`.github/dependabot.yml`, `pyproject.toml`/`uv.lock`, GitHub branch protection, and
`.github/PUBLIC_RELEASE_CHECKLIST.md`. No workflow, release config, or dependency
specifier was modified as part of this task.

## 1. CI workflow inventory

Commands run: `gh run list --branch main --limit 20`; each file under
`.github/workflows/` read in full.

| Workflow (job) | File | Trigger | What it checks | Most recent run on `main` |
|---|---|---|---|---|
| CI — `lint · types · tests · docs` | `ci.yml` (`check`) | `push` to `main`, every `pull_request` | ruff lint, ruff format check, mypy strict, `scripts/check_docs_consistency.py`, non-live/non-postgres pytest suite w/ coverage | success (run 33330284848, 2026-08-30T19:13:56Z, 2m39s) |
| CI — `postgres integration harness` | `ci.yml` (`postgres`) | same as above | pytest `-m postgres` against a real `postgres:16` service container (loopback-only) | bundled in the same `CI` check run above; success |
| CI — `keycloak integration harness` | `ci.yml` (`keycloak`) | same as above | pytest `-m keycloak` against a real Keycloak 26 container (realm imported from `docker/keycloak-test/realm-export.json`) | bundled in the same `CI` check run above; success |
| CI — `combined coverage gate` | `ci.yml` (`coverage`) | needs `[check, postgres, keycloak]` | combines the three jobs' coverage data, `--fail-under=90` | bundled in the same `CI` check run above; success |
| CI — `build · clean-install smoke` | `ci.yml` (`package`) | same as above | `uv build`, then `scripts/release_smoke.sh` against the built wheel in isolation | bundled in the same `CI` check run above; success — this job's name is one of the four **required** branch-protection contexts |
| CodeQL — `analyze Python` | `codeql.yml` | `push` to `main`, every `pull_request`, weekly cron (`17 9 * * 1`) | CodeQL static analysis for Python | success (run 33330284802, 2026-08-30T19:13:56Z, 1m32s). Job is gated `if: github.event.repository.visibility == 'public'` — the repo went public 2026-08-27, so this now actually executes rather than being skipped, matching the checklist's claim |
| Secret scan — `gitleaks history scan` | `secret-scan.yml` | `push` to `main`, every `pull_request` | `gitleaks/gitleaks-action` full-history scan (`fetch-depth: 0`) | success (run 33330284801, 2026-08-30T19:13:56Z, 13s) |
| Live n8n compatibility — `real instance contract` | `live-n8n.yml` | `workflow_dispatch` only (manual) | `pytest -m live_n8n` against a real n8n instance via the `live-n8n` GitHub Environment (secrets/vars) | not in the last-20 `main` list (never runs on push — by design, per `ci.yml`'s comment that live-n8n tests are opt-in and never run in CI). Evidence of a real invocation is tracked separately in `docs/evidence/stage11-live-n8n-run.md` |
| Release | `release.yml` | `push` of a `v*` tag only — no `workflow_dispatch`, no branch push, no PR | see section 3 below | not exercised on a plain `main` push (tag-only trigger); last tag-triggered run was `v1.0.0rc3` per the workflow's own header comment, not captured by the branch-scoped `gh run list` query above |
| Dependency Graph / Dependabot Updates | (GitHub-managed, config in `.github/dependabot.yml`) | dynamic (Dependabot) | weekly `uv` (production/dev grouped) and `github-actions` update PRs | success (runs 33328245028 and 33328192251, 2026-08-30) |

`gh run list --branch main --limit 20` output (most recent 20 runs) shows only
successes back through stage 05 (2026-08-29T20:36:08Z) — no failing or in-progress run
on `main` in that window.

## 2. Branch protection on `main`

Command run: `gh api repos/katekruger/n8n-operator/branches/main/protection`.

- **Required status checks** (`strict: true` — branches must be up to date before
  merging): `lint · types · tests · docs`, `build · clean-install smoke`,
  `gitleaks history scan`, `analyze Python`. These map 1:1 to the CI `check` job, the
  CI `package` job, the Secret-scan `gitleaks` job, and the CodeQL `analyze` job
  respectively — all four are `app_id: 15368` (GitHub Actions).
- **`enforce_admins`**: `true` — repository admins are not exempt from required checks.
- **`allow_force_pushes`**: `false`.
- **`allow_deletions`**: `false`.
- **`required_signatures`**: `false` (commit signing not enforced).
- **`required_linear_history`**: `false`.
- **`required_conversation_resolution`**: `false`.
- **`block_creations`**: `false`; **`lock_branch`**: `false`; **`allow_fork_syncing`**:
  `false`.

This matches `.github/PUBLIC_RELEASE_CHECKLIST.md`'s GitHub-controls claim verbatim:
"Branch protection configured on `main`: `lint · types · tests · docs`,
`build · clean-install smoke`, `gitleaks history scan`, and `analyze Python` (CodeQL)
required and must be up to date; `enforce_admins` on; force pushes and branch deletion
disabled." No discrepancy found. Note the three `postgres`, `keycloak`, and `coverage`
jobs in `ci.yml` are **not** in the required-checks list by name — only `check` (the
job literally named `lint · types · tests · docs`) is required. Since `coverage` job
`needs: [check, postgres, keycloak]`, a PR could theoretically merge if `check` and
`package` pass while `postgres`/`keycloak`/`coverage` are still running or were
individually disabled — in practice all jobs run together under one `CI` workflow
trigger and `gh run list` shows them completing together, so this is a structural
observation, not an observed failure mode.

## 3. `release.yml` provenance job and job ordering

Full job graph (`needs:` edges), read directly from `release.yml`:

```
verify  →  provenance  →  github-release  →  pypi (currently if: false, disabled)
```

- `verify` (`verify · build · inspect`): runs `check_release_consistency.py`, lint,
  format check, mypy, docs-consistency, the full non-live test suite gated at
  `--cov-fail-under=90`, `uv build`, `scripts/release_smoke.sh`, the OpenAI-compatible
  Streamable-HTTP MCP test, and `scripts/inspect_release_artifacts.sh`. Uploads
  `dist/` as the `release-dist` artifact.
- `provenance` (`attest build provenance`): `needs: verify`. Permissions are scoped to
  exactly `id-token: write`, `attestations: write`, `contents: read` — no broader
  write access. It downloads the `release-dist` artifact produced by `verify`, then
  calls `actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8`
  (pinned to a full commit SHA, tagged `# v4.2.2`) with `subject-path: "dist/*"`. This
  is the correct, standard usage — it attests every file under `dist/` (both the wheel
  and sdist that `uv build` produces) and does not skip either.
- `github-release`: `needs: [verify, provenance]` — **the provenance job is a hard
  dependency of the publish job**, i.e. it gates rather than follows publication. It
  runs under the `release` GitHub Environment and downloads the same `release-dist`
  artifact (not a re-build), so the exact bytes that were attested are what's actually
  published in the GitHub Release.
- `pypi`: `needs: github-release`, currently short-circuited with `if: false` because
  no PyPI trusted publisher is registered yet (documented inline and in the checklist).
  When enabled, it inherits the same ordering — `provenance` still runs before it
  because `pypi` transitively depends on `github-release` which depends on
  `provenance`.

**Finding: correct ordering.** `provenance` sits strictly between `verify` (which
produces the artifact) and both publish jobs (`github-release`, `pypi`), so attestation
happens on the built artifact before either publish step, not after. This matches the
task's requirement and the checklist's own claim ("Build provenance attested … the
`provenance` job, before either publish step runs").

Both `actions/attest-build-provenance` and all other third-party Actions referenced in
`release.yml` (`actions/checkout`, `astral-sh/setup-uv`, `actions/download-artifact`,
`pypa/gh-action-pypi-publish`) are pinned by full commit SHA with a version comment,
consistent with the rest of the workflow set (`ci.yml`, `codeql.yml`, `secret-scan.yml`,
`live-n8n.yml` all pin the same way).

PyPI publishing uses OIDC trusted publishing (`id-token: write`, no stored PyPI API
token secret) per the workflow's own header comment — the `pypi` job's only permission
is `id-token: write`.

## 4. Dependency specifiers (`pyproject.toml`)

All sixteen runtime dependencies use a lower-bound-and-upper-bound (`>=X,<Y`) "compatible
release" range rather than an exact pin — this is normal/expected for a library
`pyproject.toml` (as opposed to an application's exact-pinned lockfile) and every range
excludes the next major version:

```
mcp>=2.1.1,<3
pydantic>=2.13,<3
pydantic-settings>=2.7,<3
fastapi>=0.141,<1
uvicorn[standard]>=0.38,<1
jinja2>=3.1,<4
sqlalchemy>=2.0.52,<3
alembic>=1.19,<2
httpx>=0.28,<1
typer>=0.27,<1
rich>=13.9,<16
pyyaml>=6.0,<7
jsonschema>=4.23,<5
jsonpath-ng>=1.7,<2
python-ulid>=3.0,<5
cryptography>=43,<47
```

No dependency is fully unbounded (e.g. no bare `>=X` with no ceiling), so none of them
admit an unbounded-major surprise. `rich` (`<16` against a current `13.9` floor) and
`python-ulid` (`<5` against a `3.0` floor) are the widest windows — each spans more than
one major version — which is a normal risk-acceptance choice for those specific
libraries (both are low blast-radius, non-security-critical) but is worth naming
explicitly as the widest ranges in the dependency set.

**What actually prevents a surprise transitive update between CI runs is not the
`pyproject.toml` ranges but `uv.lock` plus consistent `--frozen`/`UV_FROZEN` usage**,
confirmed by reading all four workflows that install dependencies:

- `ci.yml` sets `env: UV_FROZEN: "1"` at the workflow level, so every `uv sync
  --all-extras --dev` call in it (in `check`, `postgres`, `keycloak`, `coverage`, and
  `package` jobs) is frozen against the committed `uv.lock` without needing `--frozen`
  spelled out per-step.
- `release.yml`'s `verify` job calls `uv sync --frozen --all-extras --dev` explicitly.
- `live-n8n.yml` calls `uv sync --frozen --all-extras --dev` explicitly.

All three workflows resolve to the same committed `uv.lock`, so a `release.yml` run
installs exactly what `ci.yml` already tested — no separate resolution step, and no
window in which a floating range could resolve differently between a CI run and a
release run. This is the mechanism that closes the gap the wide `pyproject.toml` ranges
would otherwise leave open; the ranges alone would not.

This audit did not find a Task 5 report file
(`.superpowers/sdd/2026-08-30-stage-11-v2-integration-release-and-proof/task-5-report.md`)
in the repo at the time of this pass, so there was nothing to cross-reference; Task 5
appears to have run as `task-3a`/`task-3b` in this plan's numbering, and neither of
those reports mentions dependency pinning. If Task 5's supply-chain probe lands
separately, this section's `uv.lock`/`--frozen` finding should be cross-checked against
it rather than duplicated.

## 5. `.github/dependabot.yml`

Two ecosystems configured, both on a weekly schedule: `uv` (directory `/`, grouped into
`python-runtime` production and `python-development` dev-dependency PRs) and
`github-actions` (directory `/`, ungrouped). This is consistent with the checklist's
"Dependabot alerts reviewed; zero open alerts, zero open PRs" claim — this audit did not
independently re-verify the zero-open-alerts state (that requires the Dependabot alerts
UI/API, not the workflow files), so that specific claim is taken on the checklist's word,
not re-confirmed here.

## Summary — no gaps found

- CI, CodeQL, and Secret-scan workflows are all green on the latest `main` commit as of
  this audit.
- Branch protection matches the checklist's claim exactly: the four named contexts
  required, `enforce_admins` on, force-push and deletion blocked.
- `release.yml`'s `provenance` job correctly gates both publish jobs and uses
  `actions/attest-build-provenance` on the actual built `dist/*` artifacts, pinned by
  commit SHA.
- No unbounded/floating dependency specifier in `pyproject.toml`; the widest ranges
  (`rich`, `python-ulid`) are named above for visibility, and the real
  reproducibility guarantee comes from `uv.lock` + `--frozen`/`UV_FROZEN`, verified
  consistent across all three dependency-installing workflows.

No genuine release-process gap was found that would warrant modifying CI or release
configuration as part of this task.
