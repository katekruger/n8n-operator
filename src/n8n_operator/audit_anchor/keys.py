"""Ed25519 keypair generation and file-based private-key storage (ADR-012 section 2).

The private key is a persistent asymmetric identity meant to survive process restarts
and support rotation history — a different shape of secret than the short opaque
values (an n8n API key, a webhook bearer token) ``config.resolve_secret_reference``'s
``env:``/``keyring:`` indirection was built for (ADR-006). It gets its own storage
mechanism instead: a dedicated file, generated once by ``n8n-operator anchor
init-key``, permissioned ``0600``, and never routed through the database (ADR-012
section 2's own explicit requirement — "never store a private signing key in the
database").

No import from ``core/``, ``storage/``, or any other capability package — this module
depends only on the standard library and ``cryptography`` (ARCHITECTURE.md section 2.1).
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

__all__ = [
    "InvalidSignature",
    "KeyFileExistsError",
    "generate_keypair",
    "load_private_key",
    "load_public_key",
    "public_key_b64",
    "save_private_key",
]


class KeyFileExistsError(Exception):
    """Raised by :func:`save_private_key` when ``path`` already holds a key — refusing
    to silently overwrite key material is the point; a caller that genuinely wants to
    rotate must remove or move the old file first, a deliberate action, not a default."""


def generate_keypair() -> tuple[bytes, bytes]:
    """A fresh ``(private_key_bytes, public_key_bytes)`` pair — raw 32-byte Ed25519
    encodings, not PEM (nothing here needs certificate tooling; base64-encoding the raw
    bytes is enough for both file storage and the anchor payload)."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return _private_bytes(private_key), _public_bytes(public_key)


def _private_bytes(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


def save_private_key(path: Path, private_key_bytes: bytes) -> None:
    """Write the raw private key bytes (base64-encoded) to ``path`` with ``0600``
    permissions, refusing to overwrite an existing file (:class:`KeyFileExistsError`).
    ``O_EXCL`` makes the existence check and the creation atomic — no window between
    "check it doesn't exist" and "create it" for a second, concurrent
    ``init-key`` to race through."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise KeyFileExistsError(f"key file already exists: {path}") from exc
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64encode(private_key_bytes))
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def load_private_key(path: Path) -> Ed25519PrivateKey:
    raw = base64.b64decode(path.read_bytes())
    return Ed25519PrivateKey.from_private_bytes(raw)


def load_public_key(b64: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(b64))


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    return base64.b64encode(_public_bytes(private_key.public_key())).decode("ascii")
