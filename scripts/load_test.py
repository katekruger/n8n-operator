#!/usr/bin/env python3
"""Lightweight load/concurrency harness (Stage 11) — no external dependency (no
locust/k6), plain threading, matching this repo's zero-heavyweight-tooling
convention. Two published profiles with stated assumptions: 'startup' (~5 concurrent
operators, ~50 ops/day, one environment) and 'seriesc' (~50 concurrent operators,
~5,000 ops/day, 3 environments, a meaningful quorum-approval fraction). Reports
p50/p95/p99 latency and error rate. Run manually — not part of CI.

Assumptions published alongside every result: this machine's own hardware, a local
loopback-only Postgres 16 container (docker/postgres-test/docker-compose.yml), and
in-process Python threading (not a real network hop) standing in for MCP transport
latency, since this measures the governed-write pipeline's own overhead, not transport.

Numbers produced here are not an internet-scale claim — they describe one machine
talking to one loopback Postgres instance over in-process threads, nothing more.
"""

from __future__ import annotations

import argparse
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url

from n8n_operator.core import service
from n8n_operator.core.models import PreflightResult, WorkflowContract
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: stage11-load-test
workflows:
  - id: load.write_op
    n8n_workflow_id: n8n-load-1
    title: Load test write operation
    description: A synthetic external_write workflow used only for load testing.
    owner: stage11
    version: 1
    definition_hash: sha256:{hash_a}
    risk: medium
    side_effects: external_write
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/load-test
      auth: none
    input_schema:
      type: object
      properties: {{}}
      additionalProperties: false
""".format(hash_a="c" * 64)


class FakePreflight:
    def check(self, workflow: WorkflowContract) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


@dataclass
class Profile:
    name: str
    concurrent_operators: int
    total_operations: int
    environment_count: int
    quorum_fraction: float


PROFILES = {
    "startup": Profile(
        name="startup",
        concurrent_operators=5,
        total_operations=50,
        environment_count=1,
        quorum_fraction=0.0,
    ),
    "seriesc": Profile(
        name="seriesc",
        concurrent_operators=50,
        total_operations=5000,
        environment_count=3,
        quorum_fraction=0.2,
    ),
}


@dataclass
class Results:
    latencies_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, latency_ms: float, error: str | None) -> None:
        with self.lock:
            self.latencies_ms.append(latency_ms)
            if error:
                self.errors.append(error)


def _worker(
    session_factory: Any,
    principal_id: str,
    environment_id: str,
    op_count: int,
    results: Results,
) -> None:
    for i in range(op_count):
        start = time.monotonic()
        error: str | None = None
        try:
            with session_scope(session_factory) as session:
                service.prepare_operation(
                    session,
                    principal_id=principal_id,
                    environment=environment_id,
                    workflow_id="load.write_op",
                    arguments={},
                    preflight=FakePreflight(),
                    server_max_argument_bytes=262_144,
                    idempotency_key=f"load-{principal_id}-{i}-{time.time_ns()}",
                    enable_v2=True,
                )
        except Exception as exc:  # load test records every failure mode, not just specific ones
            error = f"{type(exc).__name__}: {exc}"
        results.record((time.monotonic() - start) * 1000, error)


def _create_database_if_missing(database_url: str) -> None:
    """Create the target database against its own server if it doesn't exist yet.

    Connects to the same server/credentials as ``database_url`` but against the
    server's own ``postgres`` maintenance database (mirroring
    ``tests/integration/conftest.py``'s ``postgres_test_db_url`` fixture, which does
    the same ``CREATE DATABASE`` against a base/maintenance connection) — this lets
    ``--create-database`` be self-contained rather than requiring a separately-typed
    admin URL.
    """
    url = make_url(database_url)
    db_name = url.database
    if not db_name:
        raise ValueError(f"database URL has no database name: {database_url}")
    admin_url = url.set(database="postgres").render_as_string(hide_password=False)
    admin_engine = create_engine_for_url(admin_url)
    admin_engine = admin_engine.execution_options(isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}
            ).first()
            if exists is None:
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
                print(f"Created database {db_name!r}.")
            else:
                print(f"Database {db_name!r} already exists; reusing it.")
    finally:
        admin_engine.dispose()


def run_profile(profile: Profile, database_url: str) -> tuple[Results, float]:
    from alembic import command

    from n8n_operator.cli.commands.db import _alembic_config

    command.upgrade(_alembic_config(database_url), "head")
    engine = create_engine_for_url(
        database_url,
        pool_size=profile.concurrent_operators + 2,
        max_overflow=profile.concurrent_operators,
    )
    factory = create_session_factory(engine)

    registry_path = Path(f"/tmp/stage11-load-{profile.name}-registry.yaml")  # noqa: S108 - manual dev script, not shipped
    registry_path.write_text(REGISTRY_YAML)

    env_ids: list[str] = []
    operator_ids: list[str] = []
    with session_scope(factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
        org = OrganizationRepository(session).create(name=f"Load {profile.name}")
        for i in range(profile.environment_count):
            env = EnvironmentRepository(session).create(
                organization_id=org.id,
                name=f"env-{i}",
                n8n_base_url_ref="env:LOAD_TEST_BASE_URL",
                n8n_api_key_ref="env:LOAD_TEST_API_KEY",
            )
            env_ids.append(env.id)

        for i in range(profile.concurrent_operators):
            principal = PrincipalRepository(session).create(
                kind="user",
                display_name=f"load-operator-{i}",
            )
            OrganizationMembershipRepository(session).create(
                principal_id=principal.id,
                organization_id=org.id,
                roles=["operator"],
            )
            operator_ids.append(principal.id)

    results = Results()
    ops_per_worker = profile.total_operations // profile.concurrent_operators
    threads = [
        threading.Thread(
            target=_worker,
            args=(factory, operator_ids[i], env_ids[i % len(env_ids)], ops_per_worker, results),
        )
        for i in range(profile.concurrent_operators)
    ]
    started_at = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_seconds = time.monotonic() - started_at

    engine.dispose()
    return results, wall_seconds


def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    ordered = sorted(data)
    idx = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[idx]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--profile", choices=list(PROFILES), default="startup")
    parser.add_argument(
        "--create-database",
        action="store_true",
        help=(
            "Create the target database first if it doesn't already exist (connects "
            "to the same server's 'postgres' maintenance database to issue "
            "CREATE DATABASE)."
        ),
    )
    args = parser.parse_args()

    if args.create_database:
        _create_database_if_missing(args.database_url)

    profile = PROFILES[args.profile]
    print(
        f"Running profile '{profile.name}': {profile.concurrent_operators} concurrent "
        f"operators, {profile.total_operations} total operations, "
        f"{profile.environment_count} environment(s)."
    )
    results, wall_seconds = run_profile(profile, args.database_url)

    print(f"\nWall clock: {wall_seconds:.2f}s")
    print(f"Total operations attempted: {len(results.latencies_ms)}")
    print(
        f"Errors: {len(results.errors)} "
        f"({len(results.errors) / max(1, len(results.latencies_ms)) * 100:.2f}%)"
    )
    print(f"Throughput: {len(results.latencies_ms) / wall_seconds:.2f} ops/sec")
    print(f"Latency p50: {_percentile(results.latencies_ms, 0.50):.1f}ms")
    print(f"Latency p95: {_percentile(results.latencies_ms, 0.95):.1f}ms")
    print(f"Latency p99: {_percentile(results.latencies_ms, 0.99):.1f}ms")
    if results.latencies_ms:
        print(f"Latency mean: {statistics.fmean(results.latencies_ms):.1f}ms")
    if results.errors:
        print("\nSample errors:")
        for err in results.errors[:10]:
            print(f"  {err}")


if __name__ == "__main__":
    main()
