"""Canonical JSON and argument fingerprints.

``argument_fingerprint`` is sha256 over the canonical JSON serialization of the
arguments as submitted. This is an integrity/equality fingerprint, not a password or
credential hash: it is deliberately deterministic so prepare-time and execute-time
payloads can be compared. It provides no confidentiality, and callers must not treat it
as redaction or encryption (ADR-003).

Canonicalization must be idempotent and insensitive to key
order and insignificant whitespace, and sensitive to every structural difference —
both are Hypothesis properties (BUILD_PLAN section 10.2).

The fingerprint recorded at prepare is the fingerprint checked at execute, which is what
binds an approval to specific arguments (invariant I5).

Also home to the two limits ADR-011 fixes:

* the **core-enforced** maximum canonical argument size, applied identically for every
  adapter and **before** persistence, so an oversized payload never reaches the database
  (invariant I10, boundary B12); and
* the idempotency **namespace**, ``(principal, environment, workflow_id,
  idempotency_key)`` — same namespace and fingerprint returns the existing operation,
  same namespace and different fingerprint is ``IDEMPOTENCY_CONFLICT`` (invariant I8).

The canonical-JSON algorithm itself is not reimplemented here: it is imported from
``registry/loader.py``, which is the one place this codebase defines it (see that
module's docstring). ``core`` depending on ``registry`` is the sanctioned direction —
BUILD_PLAN section 4's layering diagram reads ``core -> registry, storage, audit, n8n``
— so this is reuse, not a layering violation; the reverse (``registry`` importing from
``core``) would be.

Phase 3 (BUILD_PLAN section 12), with the primitives below pulled forward into phase 2
because ``registry/validation.py``'s effective-argument-limit resolution and the
idempotency-namespace scoping both need them to exist.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any

from n8n_operator.errors import ArgumentsTooLargeError, IdempotencyConflictError
from n8n_operator.registry.loader import canonical_json_bytes

__all__ = [
    "IdempotencyNamespace",
    "IdempotencyResolution",
    "canonicalize_arguments",
    "check_argument_size",
    "fingerprint_arguments",
    "resolve_idempotency",
]


def canonicalize_arguments(arguments: dict[str, Any]) -> bytes:
    """The canonical JSON serialization of ``arguments``, as UTF-8 bytes.

    The exact same bytes both :func:`fingerprint_arguments` and
    :func:`check_argument_size` operate on (ADR-011: "the same bytes the fingerprint is
    taken over") — there is exactly one canonical form of a given argument set, not a
    separate one for hashing and another for size-checking.
    """
    return canonical_json_bytes(arguments)


def fingerprint_arguments(canonical_bytes: bytes) -> str:
    """Return the operation's deterministic integrity/equality fingerprint.

    SHA-256 is used here for stable collision-resistant comparison, not for password
    storage or confidentiality. The preimage is the operation payload already retained
    by v1 for dispatch and execute-time verification; see ADR-003.
    """
    return "sha256:" + hashlib.sha256(canonical_bytes).hexdigest()


def check_argument_size(canonical_bytes: bytes, *, effective_limit: int) -> None:
    """Raise :class:`~n8n_operator.errors.ArgumentsTooLargeError` if ``canonical_bytes``
    exceeds ``effective_limit``.

    Called **before** any operation row is written (invariant I10): this is deliberately
    not something that produces an ``INVALID`` operation — recording the oversized
    payload at all is the thing being refused (ADR-011). ``effective_limit`` is the
    caller's responsibility to compute (the server ceiling, or a workflow's own lower
    ``limits.max_argument_bytes`` override) — this function only compares.
    """
    size = len(canonical_bytes)
    if size > effective_limit:
        raise ArgumentsTooLargeError(
            details={"size": size, "limit": effective_limit},
        )


@dataclass(frozen=True)
class IdempotencyNamespace:
    """The four-part scope invariant I8 and ADR-011 define idempotency over.

    Two requests collide only when *all four* match. This is a plain data carrier for
    the namespace — the scoping itself is enforced where it matters, by the
    ``(principal_id, environment, workflow_id, idempotency_key)`` unique constraint on
    ``operations`` (``storage/models.py``) and by
    :meth:`~n8n_operator.storage.repository.OperationRepository.find_by_idempotency`,
    which both take all four components explicitly rather than three or two.
    """

    principal_id: str
    environment: str
    workflow_id: str
    idempotency_key: str


class IdempotencyResolution(Enum):
    """What a ``prepare_operation`` caller should do, given whether a prior operation
    already exists in this namespace under this key."""

    NEW = "new"
    """No prior operation in this namespace/key — proceed to create one."""

    REPLAY = "replay"
    """A prior operation exists with the same argument fingerprint — return it,
    ``idempotent_replay: true``. No second operation is created."""


def resolve_idempotency(
    *, existing_fingerprint: str | None, new_fingerprint: str
) -> IdempotencyResolution:
    """Decide what an idempotency-namespace lookup means for this request.

    ``existing_fingerprint`` is the ``argument_fingerprint`` of whatever operation (if
    any) :meth:`OperationRepository.find_by_idempotency` already found for this exact
    ``(principal, environment, workflow_id, idempotency_key)`` — the namespace lookup
    itself is the caller's job; this function only compares the two fingerprints:

    * no prior operation (``existing_fingerprint is None``) → :attr:`IdempotencyResolution.NEW`
    * prior operation, same fingerprint → :attr:`IdempotencyResolution.REPLAY`
    * prior operation, a *different* fingerprint → raises
      :class:`~n8n_operator.errors.IdempotencyConflictError` (ADR-011 section 3, which
      superseded the phase-0 error-code spelling — see ``errors.py`` and
      ``tests/contract/test_error_taxonomy.py`` for the exact old/new names)
    """
    if existing_fingerprint is None:
        return IdempotencyResolution.NEW
    if existing_fingerprint == new_fingerprint:
        return IdempotencyResolution.REPLAY
    raise IdempotencyConflictError(
        details={"existing_fingerprint": existing_fingerprint, "new_fingerprint": new_fingerprint},
    )
