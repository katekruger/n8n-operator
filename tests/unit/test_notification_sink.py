"""``core.service._deliver_with_dedup``/``retry_failed_notifications`` (ADR-018;
stage 05) against a real database — dedup by idempotency key, bounded retry to
``DELIVERY_FAILED``, and content redaction on ``NotificationEvent`` itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import DeliveryOutcome, NotificationEvent
from n8n_operator.storage.repository import PrincipalRepository
from n8n_operator.storage.session import session_scope


class _RecordingSink:
    def __init__(self, *, outcomes: list[bool] | None = None) -> None:
        self.calls: list[NotificationEvent] = []
        self._outcomes = list(outcomes) if outcomes is not None else None

    def deliver(self, event: NotificationEvent) -> DeliveryOutcome:
        self.calls.append(event)
        if self._outcomes is not None:
            return DeliveryOutcome(delivered=self._outcomes.pop(0))
        return DeliveryOutcome(delivered=True)


def _event(subject_id: str = "op_1", principal_id: str | None = None) -> NotificationEvent:
    return NotificationEvent(
        event_type="approval.requested",
        subject_type="operation",
        subject_id=subject_id,
        principal_id=principal_id,
        occurred_at=datetime.now(UTC),
        fetch_reference=f"n8n-operator operations approval-status {subject_id}",
    )


def test_dedup_never_calls_the_sink_twice_for_the_same_key(
    session_factory: sessionmaker[Session],
) -> None:
    sink = _RecordingSink()
    event = _event()
    with session_scope(session_factory) as session:
        first = service._deliver_with_dedup(session, sink=sink, event=event)
        second = service._deliver_with_dedup(session, sink=sink, event=event)
    assert first.delivered is True
    assert second.delivered is True
    assert first.idempotency_key == second.idempotency_key
    assert len(sink.calls) == 1  # the sink itself was invoked exactly once


def test_distinct_principals_on_the_same_subject_each_get_their_own_delivery(
    session_factory: sessionmaker[Session],
) -> None:
    sink = _RecordingSink()
    with session_scope(session_factory) as session:
        principal_a = PrincipalRepository(session).create(kind="user", display_name="A").id
        principal_b = PrincipalRepository(session).create(kind="user", display_name="B").id
        service._deliver_with_dedup(session, sink=sink, event=_event(principal_id=principal_a))
        service._deliver_with_dedup(session, sink=sink, event=_event(principal_id=principal_b))
    assert len(sink.calls) == 2


def test_bounded_retry_marks_delivery_failed_after_max_attempts(
    session_factory: sessionmaker[Session],
) -> None:
    sink = _RecordingSink(outcomes=[False])
    with session_scope(session_factory) as session:
        receipt = service._deliver_with_dedup(session, sink=sink, event=_event())
    assert receipt.delivered is False

    # Two more sweeps (max_attempts=3): first still fails, second exhausts attempts
    # and becomes permanently `failed` — never retried again.
    with session_scope(session_factory) as session:
        count = service.retry_failed_notifications(
            session, sink=_RecordingSink(outcomes=[False]), max_attempts=3
        )
    assert count == 0
    with session_scope(session_factory) as session:
        count = service.retry_failed_notifications(
            session, sink=_RecordingSink(outcomes=[False]), max_attempts=3
        )
    assert count == 0

    # A fourth sweep, even with a sink that would now succeed, delivers nothing more
    # — the row is `failed`, not `pending`, and is never picked up again.
    with session_scope(session_factory) as session:
        count = service.retry_failed_notifications(
            session, sink=_RecordingSink(outcomes=[True]), max_attempts=3
        )
    assert count == 0


def test_bounded_retry_delivers_a_pending_row_that_now_succeeds(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        service._deliver_with_dedup(session, sink=_RecordingSink(outcomes=[False]), event=_event())
    with session_scope(session_factory) as session:
        count = service.retry_failed_notifications(
            session, sink=_RecordingSink(outcomes=[True]), max_attempts=5
        )
    assert count == 1


@pytest.mark.parametrize("forbidden_field", ["arguments", "title", "description", "result"])
def test_notification_event_never_carries_operation_content(forbidden_field: str) -> None:
    """ADR-018 section 4: only ``event_type``, ``subject_id``, ``principal_id``,
    ``occurred_at``, ``fetch_reference`` — never operation arguments, a workflow's
    title/description, or an execution result."""
    event = _event()
    assert not hasattr(event, forbidden_field)
    assert set(type(event).model_fields) == {
        "event_type",
        "subject_type",
        "subject_id",
        "principal_id",
        "occurred_at",
        "fetch_reference",
    }
