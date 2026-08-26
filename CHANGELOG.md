# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — phase 0: architecture and bootstrap

- Product definition, v1/v2/v3 outcomes, and exact feature boundaries
  (`docs/BUILD_PLAN.md`).
- Operation state machine: twelve states, fifteen transitions, eight invariants.
- Workflow registry schema with ten load-time validation rules.
- MCP tool inventory: 12 tools in v1, 20 in v2, 28 in v3.
- Storage model for SQLite (v1) and PostgreSQL (v2).
- Security boundaries B1–B11 and a full STRIDE threat model with LLM-specific threats.
- Test strategy, acceptance criteria AC-01–AC-25, and a per-phase progress checklist.
- Seven architecture decision records (ADR-001 … ADR-007).
- Python 3.12 / uv / src-layout package skeleton with dependencies pinned:
  MCP Python SDK v2, Pydantic v2, FastAPI, SQLAlchemy 2, Alembic, httpx, Typer,
  pytest, Hypothesis.
- Documentation consistency checker, run in CI and as a contract test.
- Annotated example workflow registry.

Nothing in this release implements product functionality.
