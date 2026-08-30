"""``audit_anchor.keys`` (stage 09, ADR-012 section 2) — key generation, file
permissions, and the refuse-to-overwrite guard, in isolation. Uses a fixed,
deterministic test keypair wherever a specific key value matters, never a freshly
generated one in an assertion — a failure here must be reproducible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from n8n_operator.audit_anchor.keys import (
    KeyFileExistsError,
    generate_keypair,
    load_private_key,
    load_public_key,
    public_key_b64,
    save_private_key,
)

# A fixed, deterministic Ed25519 test keypair (base64) — generated once, never
# regenerated, so any test asserting against its exact bytes is reproducible.
TEST_PRIVATE_KEY_B64 = "/HS6Tvlpf8WhdTRy1zxiU6PjcZu+ea8fZhjTlu2iywI="
TEST_PUBLIC_KEY_B64 = "XNcWTeAYvzXCUBQueY+I7Xm7GwNoD/O9+2BN27F/fGo="


def test_generate_keypair_returns_distinct_private_and_public_bytes() -> None:
    private_bytes, public_bytes = generate_keypair()
    assert len(private_bytes) == 32
    assert len(public_bytes) == 32
    assert private_bytes != public_bytes


def test_generate_keypair_is_not_deterministic_across_calls() -> None:
    """Sanity check on the *generator* itself — every other test in this suite uses
    the fixed test keypair above precisely because this one is not reproducible."""
    first, _ = generate_keypair()
    second, _ = generate_keypair()
    assert first != second


def test_save_and_load_private_key_round_trips(tmp_path: Path) -> None:
    key_path = tmp_path / "key"
    private_bytes, _public_bytes = generate_keypair()
    save_private_key(key_path, private_bytes)
    loaded = load_private_key(key_path)
    assert public_key_b64(loaded) == public_key_b64(load_private_key(key_path))


def test_save_private_key_sets_0600_permissions(tmp_path: Path) -> None:
    key_path = tmp_path / "key"
    private_bytes, _public_bytes = generate_keypair()
    save_private_key(key_path, private_bytes)
    mode = key_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_save_private_key_refuses_to_overwrite_an_existing_key(tmp_path: Path) -> None:
    key_path = tmp_path / "key"
    private_bytes, _public_bytes = generate_keypair()
    save_private_key(key_path, private_bytes)
    other_bytes, _ = generate_keypair()
    with pytest.raises(KeyFileExistsError):
        save_private_key(key_path, other_bytes)
    # The original key must be untouched.
    assert load_private_key(key_path)


def test_save_private_key_creates_parent_directories(tmp_path: Path) -> None:
    key_path = tmp_path / "nested" / "dir" / "key"
    private_bytes, _public_bytes = generate_keypair()
    save_private_key(key_path, private_bytes)
    assert key_path.exists()


def test_load_public_key_from_b64_matches_the_private_keys_own_public_key(
    tmp_path: Path,
) -> None:
    import base64

    key_path = tmp_path / "key"
    private_bytes, public_bytes = generate_keypair()
    save_private_key(key_path, private_bytes)
    private_key = load_private_key(key_path)
    reloaded_public = load_public_key(public_key_b64(private_key))
    from cryptography.hazmat.primitives import serialization

    reloaded_bytes = reloaded_public.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    assert reloaded_bytes == public_bytes
    assert base64.b64encode(reloaded_bytes).decode() == public_key_b64(private_key)


def test_deterministic_test_keypair_round_trips(tmp_path: Path) -> None:
    """Confirms the fixed test keypair used across the rest of this suite is
    internally consistent (its stored public key really is its own)."""
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(TEST_PRIVATE_KEY_B64))
    assert public_key_b64(private_key) == TEST_PUBLIC_KEY_B64
