# Public release checklist

Complete this checklist immediately before changing repository visibility or publishing
a package. Visibility and publication are separate, explicit operator decisions.

## GitHub controls

- [ ] Secret scanning enabled in **Settings → Code security and analysis**
- [ ] Push protection enabled
- [ ] Private vulnerability reporting enabled and tested from the public Security tab
- [ ] CodeQL and Secret scan workflows green on the release commit
- [ ] Dependabot alerts reviewed; no unresolved critical or high-risk finding
- [ ] Branch protection requires CI, CodeQL, and Secret scan
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

- [ ] `pyproject.toml`, `n8n_operator.__version__`, changelog, tag, and release title agree
- [ ] Wheel and source archive contain no credentials, database, or local registry
- [ ] GitHub Release notes name limitations and upgrade/rollback steps
- [ ] PyPI trusted publishing configured only for the protected release environment

## Stop conditions

Do not publish if installation from the final artifact fails, a security workflow is
red, the live-n8n contract is stale, or the documented client compatibility exceeds the
retained evidence.
