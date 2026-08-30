"""The exact shapes this package needs from ``core``'s anchor types, plus the sign/
verify primitives both implementations share.

Defined locally, rather than importing ``core.models.ChainAnchor``/``AnchorReceipt``
themselves, because capability packages must not depend on ``core/``
(ARCHITECTURE.md section 2.1) — the same "duck typing by construction, not by
convention" reasoning ``notifications/base.py`` documents for the identical situation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

__all__ = [
    "AnchorReceiptLike",
    "ChainAnchorLike",
    "anchor_canonical_bytes",
    "sign_anchor",
    "verify_signature",
]


class ChainAnchorLike(Protocol):
    """``core.models.ChainAnchor`` satisfies this structurally. Read-only
    ``@property`` members, not plain attributes — the same reason
    ``notifications.base.NotificationEventLike`` uses properties, so mypy checks this
    covariantly rather than invariantly."""

    @property
    def covers_through_seq(self) -> int: ...
    @property
    def entry_hash(self) -> str: ...
    @property
    def entry_count(self) -> int: ...
    @property
    def anchored_at(self) -> datetime: ...


class AnchorReceiptLike(Protocol):
    @property
    def implementation(self) -> str: ...
    @property
    def detail(self) -> dict[str, Any]: ...
    @property
    def signature(self) -> str: ...
    @property
    def public_key(self) -> str: ...


def anchor_canonical_bytes(anchor: ChainAnchorLike) -> bytes:
    """The exact bytes a ``ChainAnchor``'s signature covers — sorted keys, compact
    separators, no ASCII-escaping, the same recipe ``audit/chain.py``'s own
    ``entry_canonical_bytes`` uses for the identical reason: the signed bytes must be
    unambiguous and reproducible regardless of dict insertion order or a driver's own
    timestamp representation. Contains only the four ``ChainAnchor`` fields — no audit
    content ever reaches this function, because none exists on the type it's given
    (ADR-012 section 2's own "no audit content" requirement, enforced structurally by
    ``ChainAnchorLike`` never carrying an actor/subject/detail field to sign in the
    first place)."""
    anchored_at = anchor.anchored_at
    aware = anchored_at if anchored_at.tzinfo is not None else anchored_at.replace(tzinfo=UTC)
    payload = {
        "covers_through_seq": anchor.covers_through_seq,
        "entry_hash": anchor.entry_hash,
        "entry_count": anchor.entry_count,
        "anchored_at": aware.astimezone(UTC).isoformat(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sign_anchor(private_key: Ed25519PrivateKey, anchor: ChainAnchorLike) -> bytes:
    return private_key.sign(anchor_canonical_bytes(anchor))


def verify_signature(
    public_key: Ed25519PublicKey, anchor: ChainAnchorLike, signature: bytes
) -> bool:
    """``True`` iff ``signature`` is a valid Ed25519 signature over ``anchor``'s own
    canonical bytes under ``public_key`` — never raises; a malformed or mismatched
    signature is a plain ``False``, not an exception a verifying caller must
    remember to catch."""
    try:
        public_key.verify(signature, anchor_canonical_bytes(anchor))
    except InvalidSignature:
        return False
    return True
