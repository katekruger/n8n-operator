"""Authenticated HTTPS webhook anchor (ADR-012 section 2) — anchors POSTed to an
operator-controlled endpoint over TLS, receipts retained. Puts chain state on a
different host under different credentials than the primary database.

One POST per publish attempt — no retry inside this class; a bounded retry sweep, if
ever added, is a CLI/core concern layered above this port, mirroring
``notifications/webhook.py``'s own "adapters are thin, core does the work" discipline.
TLS verification is not disableable (T-26's own precedent). The payload carries the
anchor plus its Ed25519 signature and public key — so the receiving endpoint (a SIEM,
a log sink) can verify authenticity independently of the bearer token, which only
proves *transport* authorization, not that the anchor content itself is genuine.
"""

from __future__ import annotations

import base64
import hashlib
import logging

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from n8n_operator.audit_anchor.base import ChainAnchorLike, sign_anchor, verify_signature
from n8n_operator.audit_anchor.keys import load_public_key, public_key_b64
from n8n_operator.audit_anchor.local_file import AnchorReceipt, AnchorVerification

__all__ = ["HttpsWebhookAnchor"]

_logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0


class HttpsWebhookAnchor:
    """Satisfies ``core.service.AuditAnchorPort`` structurally."""

    def __init__(
        self,
        *,
        url: str,
        bearer_token: str,
        private_key: Ed25519PrivateKey,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = url
        self._bearer_token = bearer_token
        self._private_key = private_key
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def publish(self, anchor: ChainAnchorLike) -> AnchorReceipt:
        signature = sign_anchor(self._private_key, anchor)
        signature_b64 = base64.b64encode(signature).decode("ascii")
        public_key = public_key_b64(self._private_key)
        payload = {
            "covers_through_seq": anchor.covers_through_seq,
            "entry_hash": anchor.entry_hash,
            "entry_count": anchor.entry_count,
            "anchored_at": anchor.anchored_at.isoformat(),
            "signature": signature_b64,
            "public_key": public_key,
        }
        try:
            response = self._client.post(
                self._url, json=payload, headers={"Authorization": f"Bearer {self._bearer_token}"}
            )
        except httpx.HTTPError as exc:
            # Never str(exc) — an httpx transport exception can carry the request URL
            # in its own text, the same reason notifications/webhook.py never
            # surfaces one raw either.
            _logger.warning("anchor_webhook_transport_error", extra={"url": self._url})
            raise RuntimeError(f"anchor webhook transport error: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise RuntimeError(f"anchor webhook returned http_{response.status_code}")
        response_body_hash = hashlib.sha256(response.content).hexdigest()
        return AnchorReceipt(
            implementation="https_webhook",
            detail={
                "url": self._url,
                "http_status": response.status_code,
                "response_body_hash": f"sha256:{response_body_hash}",
            },
            signature=signature_b64,
            public_key=public_key,
        )

    def verify(self, anchor: ChainAnchorLike, receipt: AnchorReceipt) -> AnchorVerification:
        """A webhook anchor's own verification is signature-only: unlike the local
        file, there is no readable-back copy of what the endpoint stored (it may be a
        write-only log sink). Confirming the signature over ``anchor`` matches
        ``receipt`` is the full extent of what this implementation can check without
        querying the remote sink's own API, which is out of scope for a generic
        ``AuditAnchor`` interface — a deployment wanting stronger webhook-side
        verification implements its own, sink-specific check on top of this."""
        try:
            public_key = load_public_key(receipt.public_key)
            signature = base64.b64decode(receipt.signature)
        except Exception as exc:
            return AnchorVerification(
                ok=False, reason=f"malformed receipt: {exc}", checked_through_seq=None
            )
        if not verify_signature(public_key, anchor, signature):
            return AnchorVerification(
                ok=False, reason="signature does not verify", checked_through_seq=None
            )
        return AnchorVerification(
            ok=True, reason=None, checked_through_seq=anchor.covers_through_seq
        )

    def close(self) -> None:
        self._client.close()
