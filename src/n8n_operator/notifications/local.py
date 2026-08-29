"""A local/development notification sink — the "at least one local sink" ADR-018
names alongside the authenticated webhook implementation.

Writes one structured line per event to the process's own logger, never to stdout
directly (so it composes with whatever log handler/format the deployment already
configured — the same discipline every other structured log line in this codebase
follows, ``logging_setup.py``). Always "delivers" — there is no real failure mode for
writing a log line, so this sink exists for local development and for exercising the
approval-routing flow without standing up a real webhook receiver, not as a
production notification channel.

Phase 10 (v2) stage 05.
"""

from __future__ import annotations

import logging

from n8n_operator.notifications.base import DeliveryOutcome, NotificationEventLike

__all__ = ["LocalNotificationSink"]

_logger = logging.getLogger(__name__)


class LocalNotificationSink:
    """Satisfies ``core.service.NotificationSink`` structurally (via the composition
    root's adapter — see ``notifications/base.py``'s module docstring)."""

    def deliver(self, event: NotificationEventLike) -> DeliveryOutcome:
        _logger.info(
            "notification",
            extra={
                "event_type": event.event_type,
                "subject_type": event.subject_type,
                "subject_id": event.subject_id,
                "principal_id": event.principal_id,
                "occurred_at": event.occurred_at.isoformat(),
                "fetch_reference": event.fetch_reference,
            },
        )
        return DeliveryOutcome(delivered=True)
