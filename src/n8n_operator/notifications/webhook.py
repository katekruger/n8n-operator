"""Authenticated HTTPS webhook notification sink (ADR-018).

One POST per delivery attempt — no retry inside this class at all; bounded retry is
``core.service.retry_failed_notifications``'s own concern, applied uniformly across
every sink, not duplicated here (the same "adapters are thin, core does the work"
seam every other port in this codebase already establishes). TLS verification is not
disableable, matching ``n8n/client.py``'s own discipline (threat T-26) — this is an
outbound call to an operator-controlled endpoint, not a trusted internal service, so
the same care applies.

The POST body is exactly ``NotificationEvent``'s own fields — event type, subject,
principal, timestamp, and a fetch reference — never operation content (ADR-018
section 4; the caller, ``core.service``, already guarantees this by never putting
anything else on the event in the first place — this module has no opportunity to
leak what it was never given).

Phase 10 (v2) stage 05.
"""

from __future__ import annotations

import logging

import httpx

from n8n_operator.notifications.base import DeliveryOutcome, NotificationEventLike

__all__ = ["WebhookNotificationSink"]

_logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0


class WebhookNotificationSink:
    """Satisfies ``core.service.NotificationSink`` structurally (via the composition
    root's adapter). ``bearer_token`` is a server-owned credential the operator
    configures once (``env:``/``keyring:`` indirection resolved by the composition
    root before construction, ADR-006) — never logged, never echoed in an error."""

    def __init__(
        self,
        *,
        url: str,
        bearer_token: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = url
        self._bearer_token = bearer_token
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def deliver(self, event: NotificationEventLike) -> DeliveryOutcome:
        payload = {
            "event_type": event.event_type,
            "subject_type": event.subject_type,
            "subject_id": event.subject_id,
            "principal_id": event.principal_id,
            "occurred_at": event.occurred_at.isoformat(),
            "fetch_reference": event.fetch_reference,
        }
        try:
            response = self._client.post(
                self._url,
                json=payload,
                headers={"Authorization": f"Bearer {self._bearer_token}"},
            )
        except httpx.HTTPError as exc:
            # Never str(exc) — an httpx transport exception can carry the request URL
            # (and, for a misconfigured client, headers) in its own text, the same
            # reason n8n/client.py never surfaces one raw either.
            _logger.warning("webhook_notification_transport_error", extra={"url": self._url})
            return DeliveryOutcome(delivered=False, detail=type(exc).__name__)
        if response.status_code >= 400:
            return DeliveryOutcome(delivered=False, detail=f"http_{response.status_code}")
        return DeliveryOutcome(delivered=True)

    def close(self) -> None:
        self._client.close()
