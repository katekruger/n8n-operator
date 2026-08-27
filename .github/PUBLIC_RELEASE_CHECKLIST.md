# Public release checklist

Complete this checklist immediately before changing repository visibility or publishing
a package. Visibility and publication are separate, explicit operator decisions.

## GitHub controls

- [ ] Secret scanning enabled in **Settings → Code security and analysis**
- [ ] Push protection enabled
- [ ] Private vulnerability reporting enabled and tested from the public Security tab
- [ ] CodeQL and Secret scan workflows green on the release commit
- [ ] Confirm the visibility-triggered CodeQL job ran (it is intentionally skipped while private)
- [ ] Dependabot alerts reviewed; no unresolved critical or high-risk finding
- [ ] Branch protection requires CI, CodeQL, and Secret scan (unavailable on this
      repository's current GitHub plan while private — `GET .../branches/main/protection`
      and `.../rulesets` both 403 with "Upgrade to GitHub Pro or make this repository
      public." Configure this *after* the visibility change below, not before.)
- [ ] Repository topics, description, homepage, and social preview reviewed

## Product evidence

- [ ] Non-live suite, documentation contract, and 90% coverage gate green
- [ ] Distribution built and installed successfully in a clean environment
- [ ] Fresh database migration and CLI smoke test green
- [ ] Claude Desktop stdio smoke test retained as evidence
- [ ] Generic Streamable HTTP smoke test retained as evidence
- [ ] Live n8n compatibility workflow green against every version claimed in the matrix
- [ ] Hosted OpenAI connector claim matches a retained real-client test

## Release identity

- [ ] `pyproject.toml`, `n8n_operator.__version__`, changelog, tag, and release title
      agree — `scripts/check_release_consistency.py --tag <tag>`, run automatically by
      `.github/workflows/release.yml`'s `verify` job
- [ ] Wheel and source archive contain no credentials, database, or local registry —
      `scripts/inspect_release_artifacts.sh`, run automatically by the same job
- [ ] GitHub Release notes name limitations and upgrade/rollback steps — the `verify`
      job's own gate output plus `docs/RELEASE_ROLLBACK.md`; notes are generated from
      the matching `CHANGELOG.md` section (`scripts/extract_changelog_section.py`),
      never hand-typed separately at release time
- [ ] Build provenance attested (`actions/attest-build-provenance`, Sigstore-backed) —
      the `provenance` job, before either publish step runs
- [ ] `release` and `pypi` GitHub Environments exist with required reviewers configured
      (Settings → Environments — not yet created; needs a paid plan on this repository
      while private, same constraint as branch protection above) — `release.yml`'s
      `github-release` and `pypi` jobs each target one and fail closed if it is absent
- [ ] PyPI trusted publishing configured only for the protected `pypi` environment — a
      "trusted publisher" naming this exact repository, `release.yml`, and the `pypi`
      environment must be registered on the PyPI project first (a human,
      PyPI-account-holder action; PyPI supports registering one before the project's
      first release, as a "pending publisher")

## Stop conditions

Do not publish if installation from the final artifact fails, a security workflow is
red, the live-n8n contract is stale, or the documented client compatibility exceeds the
retained evidence.
