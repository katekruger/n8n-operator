"""Use-case orchestration — the portable core (ADR-001).

Exposes prepare / approve / execute / cancel / inspect as functions over plain domain
types. Every adapter calls into here; none of them reimplements policy. Request flows
are diagrammed in ``docs/ARCHITECTURE.md`` section 4.

Phase 3 adds the operation lifecycle (prepare/approve/execute/cancel). Phase 2 adds a
first slice: the registry use cases, because ``registry validate``/``list``/``show``/
``hash`` are pure, file-only operations that need no orchestration beyond calling
``registry/loader.py`` directly, but ``registry reload`` **does** need one — persisting
a snapshot means touching both ``registry/`` and ``storage/`` at once, and only ``core/``
is permitted to depend on both (ARCHITECTURE.md section 2.1: capability modules "must
not depend on each other"). ``cli/commands/registry.py`` calls ``registry/loader.py``
directly for the four read-only commands and :func:`reload_registry` here for the one
that writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from n8n_operator.registry.loader import LoadedRegistry, load_registry
from n8n_operator.storage.models import RegistrySnapshot
from n8n_operator.storage.repository import (
    RegistrySnapshotRepository,
    WorkflowBindingRepository,
)


class AuditHook(Protocol):
    """What Phase 3's real audit writer will implement.

    No implementation exists yet (``audit/writer.py`` is still a stub) — Phase 2's
    ``reload_registry`` accepts ``audit_hook: AuditHook | None = None`` and simply does
    not call it when ``None``, which is the only behavior available before Phase 3 lands
    (per the task: "produce an audit event when audit support is available; otherwise
    define the hook used in Phase 3"). ``session`` is passed through so a real
    implementation can write its ``audit_log`` row inside the *same* transaction as the
    snapshot it is recording — required for invariant I6 once one exists.
    """

    def record_registry_reload(
        self,
        session: Session,
        *,
        previous_snapshot_id: str | None,
        new_snapshot_id: str,
        content_hash: str,
        reused_existing: bool,
    ) -> None: ...


def validate_registry(path: Path, *, server_max_argument_bytes: int) -> LoadedRegistry:
    """Load and validate the registry at ``path`` without persisting anything.

    A thin pass-through to :func:`~n8n_operator.registry.loader.load_registry` — kept
    here, rather than called directly from ``registry/loader.py`` by callers, only so
    every registry use case (including the read-only ones) has a single, consistent
    ``core.service`` entry point to import from. Raises the same exceptions
    :func:`~n8n_operator.registry.loader.load_registry` does.
    """
    return load_registry(path, server_max_argument_bytes=server_max_argument_bytes)


def get_active_snapshot(session: Session) -> RegistrySnapshot | None:
    """The currently active registry snapshot, or ``None`` before the first reload."""
    return RegistrySnapshotRepository(session).get_latest()


def reload_registry(
    session: Session,
    path: Path,
    *,
    server_max_argument_bytes: int,
    audit_hook: AuditHook | None = None,
) -> tuple[RegistrySnapshot, bool]:
    """Load, validate, and persist a new registry snapshot as the active one.

    Returns ``(snapshot, reused_existing)``: ``reused_existing`` is ``True`` when the
    loaded content hashes identically to an already-persisted snapshot, in which case
    that existing row is returned unchanged rather than creating a duplicate — snapshots
    are immutable and content-addressed, so reloading an unchanged file is a no-op at
    the storage layer (BUILD_PLAN section 6.7).

    **Validate before touching storage.** :func:`~n8n_operator.registry.loader.load_registry`
    raises before this function does anything to the database, so a registry that fails
    to load leaves the previously-active snapshot untouched — there is no code path here
    that could partially apply a bad reload.

    **Persist atomically.** The new snapshot and every one of its ``WorkflowBinding``
    rows are written inside the caller's transaction (this function does not commit —
    the caller is expected to be inside a ``session_scope`` block); a failure partway
    through leaves nothing behind rather than a half-populated snapshot.

    **Never affect already-prepared operations.** Snapshots and bindings are only ever
    inserted, never updated or deleted (``RegistrySnapshotRepository`` and
    ``WorkflowBindingRepository`` expose no such methods) — any operation that recorded
    an older ``snapshot_id`` keeps referring to valid, unchanged data indefinitely.

    Only workflow entries with ``enabled: true`` get a ``WorkflowBinding`` row: a
    disabled entry must be as unpreparable as one that was never registered at all
    (AC-01's "no signal distinguishing unregistered from nonexistent" extends naturally
    to "disabled looks exactly like absent" for binding lookups), while remaining
    present in the snapshot's own ``document`` for an audit reader.
    """
    loaded = load_registry(path, server_max_argument_bytes=server_max_argument_bytes)

    snapshot_repo = RegistrySnapshotRepository(session)
    previous = snapshot_repo.get_latest()

    existing = snapshot_repo.get_by_content_hash(loaded.content_hash)
    if existing is not None:
        if audit_hook is not None:
            audit_hook.record_registry_reload(
                session,
                previous_snapshot_id=previous.id if previous else None,
                new_snapshot_id=existing.id,
                content_hash=loaded.content_hash,
                reused_existing=True,
            )
        return existing, True

    snapshot = snapshot_repo.create(
        content_hash=loaded.content_hash,
        source_path=loaded.source_path,
        document=loaded.document,
    )

    binding_repo = WorkflowBindingRepository(session)
    for entry in loaded.entries:
        if not entry.enabled:
            continue
        assert entry.approval is not None  # loaded.entries are always resolved
        binding_repo.create(
            snapshot_id=snapshot.id,
            workflow_id=entry.id,
            n8n_workflow_id=entry.n8n_workflow_id,
            definition_hash=entry.definition_hash,
            side_effects=entry.side_effects,
            approval_policy=entry.approval,
            input_schema=entry.input_schema,
        )

    if audit_hook is not None:
        audit_hook.record_registry_reload(
            session,
            previous_snapshot_id=previous.id if previous else None,
            new_snapshot_id=snapshot.id,
            content_hash=loaded.content_hash,
            reused_existing=False,
        )

    return snapshot, False


__all__ = [
    "AuditHook",
    "get_active_snapshot",
    "reload_registry",
    "validate_registry",
]
