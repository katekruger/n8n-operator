# Rolling back a release

A published release has two independent surfaces — a GitHub Release/tag and, once
publishing is approved, a PyPI package — and each needs its own rollback. Neither
`.github/workflows/release.yml` nor any script performs any of the steps below
automatically: a rollback is a deliberate, judgment-carrying action, done by a human,
after reading what actually went wrong.

**Before doing anything else**, decide whether the bad release is *broken* (fails to
install, crashes on a documented command, a genuine regression) or *insecure* (ships a
credential, a vulnerable dependency, a security-relevant bug). An insecure release
needs a security advisory and possibly credential rotation in addition to everything
below — see [SECURITY.md](../SECURITY.md).

## GitHub Release and tag

A GitHub Release can be deleted; a **published PyPI file cannot** — see the PyPI
section below for why yanking, not deletion, is the only lever there.

First copy the exact affected tag from the GitHub Release page. Set `release_tag` below
to that exact value (for example, `v1.0.0rc3`). The deliberately invalid default makes
an unedited copy fail instead of deleting a real release. Verify the resolved release
before confirming GitHub CLI's deletion prompt.

```bash
release_tag="REPLACE_WITH_EXACT_TAG"
gh release view "$release_tag"
gh release delete "$release_tag" --cleanup-tag
```

`--cleanup-tag` also removes the underlying git tag from the remote. If you deleted the
release but kept the tag (omitted `--cleanup-tag`), remove it separately:

```bash
release_tag="REPLACE_WITH_EXACT_TAG"
git push origin ":refs/tags/$release_tag"
git tag -d "$release_tag"
```

Never force-push over or reuse a tag name that was ever public — cut a new, higher
version instead (e.g. `v1.0.0rc4` after rolling back `v1.0.0rc3`), even for a same-day
fix. A reused tag pointing at
different code than what someone already pulled is a supply-chain hazard in its own
right.

## PyPI

PyPI has no delete or overwrite: once a file is uploaded, that exact filename can never
be reused, even after removal, by design (this is what makes a "yank" safe to trust —
nothing can silently replace it later). The only correction mechanism is **yanking**:

1. Sign in to the project on pypi.org and open the release.
2. **Options → Yank release**, with a short, real reason (shown to every future
   installer).

Yanking does not delete the file — an exact pin such as
`pip install n8n-operator==1.0.0rc3` still works if
someone pins that exact version on purpose — but `pip install n8n-operator` (no pin)
and any dependency resolver skip a yanked version as if it did not exist. This is
deliberately the same behavior a bad release should have: available for forensics and
already-pinned installs, invisible to everyone else.

There is no supported PyPI API for yanking without the maintainer's own web session or
a scoped API token; `scripts/`/`release.yml` do not attempt to automate this.

## After either rollback

1. Add an `## [Unreleased]` (or the next version's) `CHANGELOG.md` entry describing
   what was wrong and what changed — never silently overwrite the bad version's own
   changelog section.
2. If the cause was a real code defect, add a regression test for it before cutting the
   replacement release — the same rule this project applies to every other bug
   (`CONTRIBUTING.md`).
3. Re-run the full [`.github/PUBLIC_RELEASE_CHECKLIST.md`](../.github/PUBLIC_RELEASE_CHECKLIST.md)
   for the replacement version. A rollback is not a reason to skip a step that would
   have caught the original problem.
