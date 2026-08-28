# Public release checklist

Complete this checklist immediately before changing repository visibility or publishing
a package. Visibility and publication are separate, explicit operator decisions.

## GitHub controls

- [x] Repository made public (2026-08-27), immediately after full-history Gitleaks
      confirmed green on the exact commit exposed (`e94f4c0`)
- [x] Secret scanning enabled in **Settings → Code security and analysis**
- [x] Push protection enabled
- [x] Private vulnerability reporting enabled (`PUT .../private-vulnerability-reporting`
      → `{"enabled": true}`) — test the public Security tab's "Report a vulnerability"
      button once, manually, from a signed-in GitHub session (not automatable)
- [x] CodeQL and Gitleaks workflows green on the `v1.0.0rc3` release commit. CodeQL
      alert 1 reviewed as a false positive: the SHA-256 value is a deterministic
      integrity/equality binding, not credential protection; the v1 threat boundary and
      future keyed-HMAC trigger are recorded in ADR-003
- [x] Confirm the visibility-triggered CodeQL job now actually runs (no longer skipped)
- [x] Dependabot alerts reviewed; zero open alerts, zero open PRs
- [x] Branch protection configured on `main`: `lint · types · tests · docs`,
      `build · clean-install smoke`, `gitleaks history scan`, and `analyze Python`
      (CodeQL) required and must be up to date; `enforce_admins` on; force pushes and
      branch deletion disabled. (Unavailable while private on this repository's GitHub
      plan — became available the moment visibility changed to public, no upgrade
      needed.)
- [x] Repository topics and description reviewed, accurate as of this pass. Homepage
      and social preview image are cosmetic, not verified here — set at your
      discretion in Settings.

## Product evidence

- [x] Non-live suite, documentation contract, and 90% coverage gate green in the
      `v1.0.0rc3` release workflow
- [x] Distribution built and installed successfully in a clean environment
- [x] Fresh database migration and CLI smoke test green
- [x] Built-wheel MCP stdio protocol session retained as automated evidence (this does
      not claim that the Claude Desktop GUI itself was launched)
- [x] OpenAI-compatible Streamable HTTP protocol test retained as automated evidence
- [ ] Live n8n compatibility workflow green against every version claimed in the matrix
- [ ] Hosted OpenAI connector claim matches a retained real-client test

## Release identity

- [x] `pyproject.toml`, `n8n_operator.__version__`, changelog, tag, and release title
      agree — `scripts/check_release_consistency.py --tag <tag>`, run automatically by
      `.github/workflows/release.yml`'s `verify` job
- [x] Wheel and source archive contain no credentials, database, or local registry —
      `scripts/inspect_release_artifacts.sh`, run automatically by the same job
- [x] GitHub Release notes name limitations and upgrade/rollback steps — the `verify`
      job's own gate output plus `docs/RELEASE_ROLLBACK.md`; notes are generated from
      the matching `CHANGELOG.md` section (`scripts/extract_changelog_section.py`),
      never hand-typed separately at release time
- [x] Build provenance attested (`actions/attest-build-provenance`, Sigstore-backed) —
      the `provenance` job, before either publish step runs
- [x] `release` and `pypi` GitHub Environments exist, each restricted to protected
      branches only (`main`) — created 2026-08-27. Required-reviewer protection is
      *not* configured (naming a specific human reviewer needs a decision only you can
      make); add one in Settings → Environments if you want a manual approval gate on
      top of `release.yml`'s automated checks before either publish step runs.
- [ ] PyPI trusted publishing configured only for the protected `pypi` environment — a
      "trusted publisher" naming this exact repository, `release.yml`, and the `pypi`
      environment must be registered on the PyPI project first (a human,
      PyPI-account-holder action; PyPI supports registering one before the project's
      first release, as a "pending publisher")

## Stop conditions

Do not publish if installation from the final artifact fails, a security workflow is
red, the live-n8n contract is stale, or the documented client compatibility exceeds the
retained evidence.
