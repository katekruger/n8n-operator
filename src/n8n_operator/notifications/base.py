"""The exact shapes this package needs from ``core``'s notification types.

Defined locally, rather than importing ``core.models.NotificationEvent``/
``DeliveryOutcome`` themselves, because capability packages must not depend on
``core/`` (ARCHITECTURE.md section 2.1) — the same "duck typing by construction, not
by convention" reasoning ``n8n/preflight.py``'s ``WorkflowLike`` documents for the
identical situation. The composition root (``mcp/server.py``) wraps a sink from this
package with a small adapter converting this local :class:`DeliveryOutcome` into
``core.models.DeliveryOutcome`` — the same pattern its ``_PreflightAdapter``/
``_HealthAdapter``/``_DispatchAdapter`` already establish for ``n8n/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

__all__ = ["DeliveryOutcome", "NotificationEventLike"]


class NotificationEventLike(Protocol):
    """``core.models.NotificationEvent`` satisfies this structurally. Read-only
    ``@property`` members, not plain attributes, so mypy checks this covariantly
    rather than invariantly — the same reason ``WorkflowLike`` uses properties."""

    @property
    def event_type(self) -> str: ...
    @property
    def subject_type(self) -> str: ...
    @property
    def subject_id(self) -> str: ...
    @property
    def principal_id(self) -> str | None: ...
    @property
    def occurred_at(self) -> datetime: ...
    @property
    def fetch_reference(self) -> str: ...


@dataclass(frozen=True)
class DeliveryOutcome:
    """Structurally identical to ``core.models.DeliveryOutcome`` — a parallel
    definition, not an import, for the same reason as :class:`NotificationEventLike`.
    ``core.service._deliver_with_dedup`` is what actually owns ``idempotency_key``
    and dedup/retry/attempt-count bookkeeping; a sink only ever reports whether
    *this one attempt* delivered."""

    delivered: bool
    detail: str | None = None
