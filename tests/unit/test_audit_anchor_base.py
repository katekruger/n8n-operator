"""``audit_anchor.base``'s sign/verify primitives (stage 09, ADR-012 section 2) —
round trip and tamper detection, using a fixed, deterministic test keypair."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from n8n_operator.audit_anchor.base import anchor_canonical_bytes, sign_anchor, verify_signature

TEST_PRIVATE_KEY_B64 = "/HS6Tvlpf8WhdTRy1zxiU6PjcZu+ea8fZhjTlu2iywI="


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(TEST_PRIVATE_KEY_B64))


@dataclass(frozen=True)
class _Anchor:
    covers_through_seq: int
    entry_hash: str
    entry_count: int
    anchored_at: datetime


def _anchor(**overrides: object) -> _Anchor:
    defaults: dict[str, object] = {
        "covers_through_seq": 42,
        "entry_hash": "sha256:" + "a" * 64,
        "entry_count": 42,
        "anchored_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return _Anchor(**defaults)  # type: ignore[arg-type]


def test_anchor_canonical_bytes_is_deterministic() -> None:
    anchor = _anchor()
    assert anchor_canonical_bytes(anchor) == anchor_canonical_bytes(anchor)


def test_anchor_canonical_bytes_differs_for_a_different_covers_through_seq() -> None:
    a1 = _anchor(covers_through_seq=1)
    a2 = _anchor(covers_through_seq=2)
    assert anchor_canonical_bytes(a1) != anchor_canonical_bytes(a2)


def test_anchor_canonical_bytes_contains_no_audit_content_fields() -> None:
    """ADR-012's own hard requirement: no actor, subject, or detail ever reaches the
    signed bytes — checked here at the byte level, not just by type shape, since a
    future edit could add a field to the payload dict without changing the type."""
    anchor = _anchor()
    payload = anchor_canonical_bytes(anchor).decode("utf-8")
    for forbidden in ("actor", "subject_id", "subject_type", "detail", "action", "outcome"):
        assert forbidden not in payload


def test_sign_then_verify_round_trips() -> None:
    private_key = _private_key()
    anchor = _anchor()
    signature = sign_anchor(private_key, anchor)
    assert verify_signature(private_key.public_key(), anchor, signature) is True


def test_verify_fails_when_the_anchor_content_changes_after_signing() -> None:
    private_key = _private_key()
    signature = sign_anchor(private_key, _anchor(covers_through_seq=1))
    tampered = _anchor(covers_through_seq=2)
    assert verify_signature(private_key.public_key(), tampered, signature) is False


def test_verify_fails_with_a_flipped_signature_byte() -> None:
    private_key = _private_key()
    anchor = _anchor()
    signature = bytearray(sign_anchor(private_key, anchor))
    signature[0] ^= 0xFF
    assert verify_signature(private_key.public_key(), anchor, bytes(signature)) is False


def test_verify_fails_under_a_different_public_key() -> None:
    private_key = _private_key()
    other_key = Ed25519PrivateKey.generate()
    anchor = _anchor()
    signature = sign_anchor(private_key, anchor)
    assert verify_signature(other_key.public_key(), anchor, signature) is False


def test_verify_never_raises_on_a_malformed_signature() -> None:
    private_key = _private_key()
    anchor = _anchor()
    assert verify_signature(private_key.public_key(), anchor, b"not-a-real-signature") is False
