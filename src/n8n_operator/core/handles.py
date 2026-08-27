"""Operation handles and approval tokens: mint, bind, verify, burn (ADR-003).

**An operation handle is the operation ID.** ADR-003 considered and rejected a separate
bearer secret ("a separate bearer secret would add a second thing to leak without adding
a check"): the ID alone confers no authority, because ``execute_operation`` also requires
the operation to be ``APPROVED``, unburnt, within its deadline, and free of definition
drift — checks a leaked ID cannot satisfy on its own. So "minting a handle" is minting a
fresh, unpredictable ``op_<ULID>``, and "binding" it is simply the fact that the
``operations`` row created with that ID carries ``principal_id``, ``workflow_id``,
``definition_hash``, and ``argument_fingerprint`` alongside it (``storage/models.py``).

ULID randomness comes from ``os.urandom`` via the ``python-ulid`` package — a CSPRNG, not
a general-purpose PRNG — so the handle is cryptographically unguessable despite embedding
a millisecond timestamp in its leading bits.

**Verification and burn are compare-and-set**, not read-then-write:
:meth:`~n8n_operator.storage.repository.OperationRepository.burn_handle` issues
``UPDATE ... WHERE handle_burned_at IS NULL`` and checks the affected-row count, so
"is this handle still usable" and "mark it used" happen as one atomic database operation.
Two concurrent callers racing to burn the same handle can never both see zero rows
affected by someone else and also succeed themselves (invariant I4) — this module does not
re-implement that guarantee, it only gives ``core/service.py`` a single, named place to
call through.

**A burnt handle is never re-minted.** There is no code path anywhere that clears
``handle_burned_at`` once set; the column has no setter beyond the compare-and-set insert
itself.

An **approval token** (BUILD_PLAN section 8.1 ``approvals.token_hash``) is a different
kind of secret: unlike the handle, its whole purpose is to authorize a state transition
(T06/T07) without requiring the caller to also know the operation ID's owning principal,
because it is handed to a human over a channel (the approval page, phase 6) that the CLI
approval channel does not need at all (ADR-010: "the CLI is the canonical approval
channel"). Because it *does* function as a bearer credential for that channel, only its
hash is ever persisted — the raw token is returned once, at mint time, and never stored.

Phase 3 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from n8n_operator.storage.models import new_ulid

__all__ = [
    "MintedApprovalToken",
    "compute_approval_binding",
    "hash_approval_token",
    "mint_approval_token",
    "mint_operation_handle",
]


def mint_operation_handle() -> str:
    """A fresh, unpredictable ``op_<ULID>`` — the operation handle (ADR-003).

    The handle *is* the operation's primary key: the caller passes this string as
    ``Operation.id`` when creating the row (``storage/models.py`` deliberately does not
    generate ``operations.id`` itself — see that model's docstring).
    """
    return f"op_{new_ulid()}"


@dataclass(frozen=True)
class MintedApprovalToken:
    """The one-time result of minting an approval token.

    ``token`` is the raw bearer secret — return it to a caller exactly once, at mint
    time, and never persist it. ``token_hash`` is what
    :class:`~n8n_operator.storage.repository.ApprovalRepository` stores, and what a
    future approval-channel adapter hashes an incoming token against to verify it.
    """

    token: str
    token_hash: str


def mint_approval_token() -> MintedApprovalToken:
    """A fresh approval token and its sha256 hash.

    ``secrets.token_urlsafe`` draws from the same OS CSPRNG a handle's ULID randomness
    does, sized at 32 bytes (256 bits) — comfortably beyond brute-force range for a
    TTL-bounded, single-use, hash-stored secret.
    """
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return MintedApprovalToken(token=token, token_hash=token_hash)


def hash_approval_token(token: str) -> str:
    """The same sha256 hash :func:`mint_approval_token` computes, exposed separately so
    a caller *verifying* a token a human or a browser presents back (rather than
    minting a fresh one) can compute the hash to look up without duplicating the
    algorithm. Never logged, and never itself persisted — only the hash it returns is."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def compute_approval_binding(
    *,
    operation_id: str,
    principal_id: str,
    argument_fingerprint: str,
    snapshot_id: str,
    definition_hash: str,
) -> str:
    """The approval token's binding fingerprint (phase 6, ADR-010): a stable digest over
    exactly the operation identity an approval must never silently drift from.

    None of these five values is ever updated on an ``operations`` row after creation —
    the binding therefore already holds structurally, by construction, for the whole of
    v1. Recording it at mint time (``prepare_operation``) and recomputing and comparing
    it at redemption (``resolve_approval_token``) turns that implicit invariant into an
    explicit, verified one: defense in depth against a future regression (a v2 retry
    path that reuses a row, a bug that repoints ``approvals.operation_id``) rather than
    a condition any current code path can actually trigger.

    Field order is fixed and each field is length-prefixed before concatenation, so
    ``("ab", "c")`` and ``("a", "bc")`` never collide onto the same digest.
    """
    parts = (operation_id, principal_id, argument_fingerprint, snapshot_id, definition_hash)
    preimage = "".join(f"{len(part)}:{part}|" for part in parts).encode("utf-8")
    return "sha256:" + hashlib.sha256(preimage).hexdigest()
