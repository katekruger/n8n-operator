"""``audit_anchor.webhook.HttpsWebhookAnchor`` (stage 09, ADR-012 section 2) — real
``httpx.MockTransport`` (never a real network call), mirroring
``tests/integration/mock_n8n.py``'s own pattern applied to a mock anchor sink.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from n8n_operator.audit_anchor.webhook import HttpsWebhookAnchor

TEST_PRIVATE_KEY_B64 = "/HS6Tvlpf8WhdTRy1zxiU6PjcZu+ea8fZhjTlu2iywI="


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(TEST_PRIVATE_KEY_B64))


@dataclass(frozen=True)
class _Anchor:
    covers_through_seq: int
    entry_hash: str
    entry_count: int
    anchored_at: datetime


def _anchor(seq: int = 10) -> _Anchor:
    return _Anchor(
        covers_through_seq=seq,
        entry_hash="sha256:" + "a" * 64,
        entry_count=seq,
        anchored_at=datetime.now(UTC),
    )


@pytest.mark.integration
def test_publish_posts_the_signed_anchor_and_bearer_token() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    sink = HttpsWebhookAnchor(
        url="https://anchors.example.com/ingest",
        bearer_token="test-token",
        private_key=_private_key(),
        client=client,
    )

    anchor = _anchor()
    receipt = sink.publish(anchor)

    assert len(captured) == 1
    request = captured[0]
    assert request.headers["authorization"] == "Bearer test-token"
    body = json.loads(request.content)
    assert body["covers_through_seq"] == anchor.covers_through_seq
    assert body["entry_hash"] == anchor.entry_hash
    assert body["signature"] == receipt.signature
    assert body["public_key"] == receipt.public_key
    # No audit content in the outbound payload — only the four ChainAnchor fields
    # plus signature/public_key.
    assert set(body.keys()) == {
        "covers_through_seq",
        "entry_hash",
        "entry_count",
        "anchored_at",
        "signature",
        "public_key",
    }
    assert receipt.implementation == "https_webhook"
    assert receipt.detail["http_status"] == 200


@pytest.mark.integration
def test_publish_raises_on_an_http_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sink = HttpsWebhookAnchor(
        url="https://anchors.example.com/ingest",
        bearer_token="test-token",
        private_key=_private_key(),
        client=client,
    )
    with pytest.raises(RuntimeError, match="http_500"):
        sink.publish(_anchor())


@pytest.mark.integration
def test_publish_raises_on_a_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("mock: connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sink = HttpsWebhookAnchor(
        url="https://anchors.example.com/ingest",
        bearer_token="test-token",
        private_key=_private_key(),
        client=client,
    )
    with pytest.raises(RuntimeError):
        sink.publish(_anchor())


@pytest.mark.integration
def test_verify_checks_the_signature_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sink = HttpsWebhookAnchor(
        url="https://anchors.example.com/ingest",
        bearer_token="test-token",
        private_key=_private_key(),
        client=client,
    )
    anchor = _anchor()
    receipt = sink.publish(anchor)
    result = sink.verify(anchor, receipt)
    assert result.ok is True
    assert result.checked_through_seq == anchor.covers_through_seq


@pytest.mark.integration
def test_verify_fails_when_the_anchor_no_longer_matches_the_receipt() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sink = HttpsWebhookAnchor(
        url="https://anchors.example.com/ingest",
        bearer_token="test-token",
        private_key=_private_key(),
        client=client,
    )
    receipt = sink.publish(_anchor(10))
    different_anchor = _anchor(20)
    result = sink.verify(different_anchor, receipt)
    assert result.ok is False


@pytest.mark.integration
def test_receipt_never_carries_the_full_response_body() -> None:
    """Content-free receipts (ADR-012 section 2): only a hash of the response body,
    never the body itself."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"secret_internal_field": "should-never-be-stored"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sink = HttpsWebhookAnchor(
        url="https://anchors.example.com/ingest",
        bearer_token="test-token",
        private_key=_private_key(),
        client=client,
    )
    receipt = sink.publish(_anchor())
    assert "secret_internal_field" not in json.dumps(receipt.detail)
    assert receipt.detail["response_body_hash"].startswith("sha256:")
