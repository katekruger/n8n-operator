"""Real SQLite-v1-shaped-plus-v2-rows -> PostgreSQL migration rehearsal (Stage 11) —
proves counts, identity mapping, audit-chain integrity, and historical operation
readability survive migration, and that rollback (restoring the pre-migration SQLite
file) leaves the source untouched. Extends the existing v1-only migration coverage in
tests/integration/postgres/test_migration.py (not modified here) with v2 rows:
organizations, environments, memberships, and a real anchored audit chain.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select

from n8n_operator.audit.writer import write as audit_write
from n8n_operator.audit_anchor.local_file import LocalFileAnchor
from n8n_operator.core import service
from n8n_operator.core.postgres_migration import migrate
from n8n_operator.storage.models import Base
from n8n_operator.storage.repository import (
    AuditLogRepository,
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
    RegistrySnapshotRepository,
)
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def sqlite_source_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'source.db'}"


def _seed_v2_fixture(sqlite_source_url: str, anchor_path: Path) -> dict[str, str]:
    from alembic import command

    from n8n_operator.cli.commands.db import _alembic_config

    command.upgrade(_alembic_config(sqlite_source_url), "head")

    # `core.service.publish_anchor`'s `AuditAnchorPort` protocol expects a receipt
    # shaped like `core.models.AnchorReceipt` (Pydantic, `.model_dump(mode="json")`),
    # not the plain dataclass `LocalFileAnchor.publish` actually returns —
    # `cli/commands/anchor.py`'s own composition root bridges that gap with
    # `_ServiceSinkAdapter`; reused here rather than re-implemented, the same way
    # `test_v2_integrated_scenario.py` reaches for the identical shape.
    from n8n_operator.cli.commands.anchor import _ServiceSinkAdapter

    engine = create_engine_for_url(sqlite_source_url)
    try:
        factory = create_session_factory(engine)
        private_key = Ed25519PrivateKey.generate()
        with session_scope(factory) as session:
            principal = PrincipalRepository(session).create(kind="local", display_name="local")
            snapshot = RegistrySnapshotRepository(session).create(
                content_hash="sha256:" + "a" * 64,
                source_path="./workflows.yaml",
                document={"apiVersion": "n8n-operator/v1", "workflows": []},
            )
            org = OrganizationRepository(session).create(name="Migration Rehearsal Org")
            env = EnvironmentRepository(session).create(
                organization_id=org.id,
                name="production",
                n8n_base_url_ref="env:REHEARSAL_BASE_URL",
                n8n_api_key_ref="env:REHEARSAL_API_KEY",
                is_production=True,
            )
            member = PrincipalRepository(session).create(
                kind="user", display_name="Rehearsal Operator"
            )
            # `publish_anchor` below is admin-gated (`_require_admin`) and, under
            # `enable_v2=True`, requires a real `principal_id` whose membership
            # carries the `admin` role — granting it here alongside `operator`
            # matches the pattern `test_v2_integrated_scenario.py` establishes for
            # anchor-publishing tests, rather than inventing a second principal.
            OrganizationMembershipRepository(session).create(
                principal_id=member.id,
                organization_id=org.id,
                roles=["operator", "admin"],
            )
            # `publish_anchor` treats an empty audit chain as a well-defined no-op
            # (nothing to anchor, no row written) — a real anchored chain needs at
            # least one entry, so record the organization's creation the same way
            # `tests/integration/postgres/_seed.py` records `registry_reload` for the
            # v1-only fixture.
            audit_write(
                AuditLogRepository(session),
                actor=principal.id,
                action="create_organization",
                subject_type="organization",
                subject_id=org.id,
                outcome="allowed",
                detail={"name": org.name},
            )
            ids = {
                "org_id": org.id,
                "env_id": env.id,
                "member_id": member.id,
                "principal_id": principal.id,
                "snapshot_id": snapshot.id,
            }

        with session_scope(factory) as session:
            sink = _ServiceSinkAdapter(LocalFileAnchor(path=anchor_path, private_key=private_key))
            row = service.publish_anchor(
                session,
                sink=sink,
                implementation="local_file",
                principal_id=ids["member_id"],
                enable_v2=True,
            )
            assert row is not None
    finally:
        engine.dispose()
    return ids


def _row_counts(url: str) -> dict[str, int]:
    engine = create_engine_for_url(url)
    try:
        factory = create_session_factory(engine)
        with factory() as session:
            return {
                name: session.execute(select(func.count()).select_from(table)).scalar_one()
                for name, table in Base.metadata.tables.items()
            }
    finally:
        engine.dispose()


def test_v2_shaped_dataset_migrates_with_verified_counts_and_intact_anchor_chain(
    sqlite_source_url: str,
    postgres_test_db_url: str,
    tmp_path: Path,
) -> None:
    anchor_path = tmp_path / "anchors.jsonl"
    ids = _seed_v2_fixture(sqlite_source_url, anchor_path)

    before = _row_counts(sqlite_source_url)
    report = migrate(source_url=sqlite_source_url, dest_url=postgres_test_db_url)
    assert report.ok

    after = _row_counts(postgres_test_db_url)
    for table_name, count in before.items():
        assert after.get(table_name, 0) == count, (
            f"{table_name}: {before[table_name]} -> {after.get(table_name)}"
        )

    engine = create_engine_for_url(postgres_test_db_url)
    try:
        factory = create_session_factory(engine)
        with factory() as session:
            org = OrganizationRepository(session).get(ids["org_id"])
            assert org is not None and org.id == ids["org_id"]
            env = EnvironmentRepository(session).get(ids["env_id"])
            assert env is not None and env.organization_id == ids["org_id"]
    finally:
        engine.dispose()

    # Audit chain integrity: the anchor file signed against SQLite content is verified
    # against the migrated Postgres database — the whole point of an external anchor
    # is that it doesn't care which database backend now holds the audit log. The
    # verifier only needs its own private key to construct a `LocalFileAnchor`
    # instance; `verify_file` checks each line's *embedded* public key, so a freshly
    # generated key here is fine — it never signs anything.
    verifier = LocalFileAnchor(path=anchor_path, private_key=Ed25519PrivateKey.generate())
    file_report = verifier.verify_file()
    assert file_report.ok
    assert file_report.lines_checked == 1
    assert file_report.issues == []


def test_rollback_restores_the_pre_migration_sqlite_file_untouched(
    sqlite_source_url: str,
    postgres_test_db_url: str,
    tmp_path: Path,
) -> None:
    _seed_v2_fixture(sqlite_source_url, tmp_path / "anchors.jsonl")

    source_path = Path(sqlite_source_url.replace("sqlite+pysqlite:///", ""))
    backup_path = tmp_path / "source-backup.db"
    shutil.copy2(source_path, backup_path)

    report = migrate(source_url=sqlite_source_url, dest_url=postgres_test_db_url)
    assert report.ok

    # "Rollback" here means: the migration never mutates the source SQLite file at
    # all (it's read-only copy semantics) — the backup and the post-migration source
    # must be byte-identical, proving there's nothing to actually restore. Rolling
    # back a bad cutover is therefore just: stop pointing the app at the new Postgres
    # database and point it back at this same, untouched SQLite file (see
    # docs/POSTGRES_OPERATIONS.md's "Rollback" section).
    assert backup_path.read_bytes() == source_path.read_bytes()
