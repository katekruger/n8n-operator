"""``UTCDateTime`` directly: the type-decorator guarantee behind ADR-004 rule D2.

Pure unit tests against the type decorator's ``process_bind_param``/``process_result_value``
methods, called the way SQLAlchemy itself calls them — no database needed. The
integration-level version of this guarantee (a real round trip through SQLite) lives in
``tests/integration/test_repository.py``, which is what first caught the bug this type
decorator exists to fix: without it, a value written with UTC tzinfo attached comes back
from SQLite naive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from n8n_operator.storage.models import UTCDateTime

PLUS_FIVE = timezone(timedelta(hours=5))


@pytest.fixture
def utc_datetime_type() -> UTCDateTime:
    return UTCDateTime()


@pytest.mark.unit
def test_process_bind_param_passes_through_none(utc_datetime_type: UTCDateTime) -> None:
    assert utc_datetime_type.process_bind_param(None, dialect=None) is None  # type: ignore[arg-type]


@pytest.mark.unit
def test_process_bind_param_rejects_a_naive_datetime(utc_datetime_type: UTCDateTime) -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)  # noqa: DTZ001 - the point of this test
    with pytest.raises(ValueError, match="naive datetime"):
        utc_datetime_type.process_bind_param(naive, dialect=None)  # type: ignore[arg-type]


@pytest.mark.unit
def test_process_bind_param_converts_a_non_utc_aware_datetime_to_utc(
    utc_datetime_type: UTCDateTime,
) -> None:
    source = datetime(2026, 1, 1, 17, 0, 0, tzinfo=PLUS_FIVE)  # == 12:00 UTC
    bound = utc_datetime_type.process_bind_param(source, dialect=None)  # type: ignore[arg-type]
    assert bound is not None
    assert bound.tzinfo == UTC
    assert bound == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.mark.unit
def test_process_result_value_passes_through_none(utc_datetime_type: UTCDateTime) -> None:
    assert utc_datetime_type.process_result_value(None, dialect=None) is None  # type: ignore[arg-type]


@pytest.mark.unit
def test_process_result_value_attaches_utc_to_a_naive_value(
    utc_datetime_type: UTCDateTime,
) -> None:
    """The exact SQLite behavior this type decorator exists to correct: a naive value
    read back from the driver is known to have been UTC, because nothing in this
    codebase ever writes anything else through this column type."""
    naive = datetime(2026, 1, 1, 12, 0, 0)  # noqa: DTZ001 - simulating the SQLite driver
    result = utc_datetime_type.process_result_value(naive, dialect=None)  # type: ignore[arg-type]
    assert result is not None
    assert result.tzinfo == UTC
    assert result == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.mark.unit
def test_process_result_value_normalizes_an_already_aware_value_to_utc(
    utc_datetime_type: UTCDateTime,
) -> None:
    """The PostgreSQL case: the driver already returns a tz-aware value (possibly not
    in UTC); it is normalized to UTC rather than left as-is."""
    aware = datetime(2026, 1, 1, 17, 0, 0, tzinfo=PLUS_FIVE)
    result = utc_datetime_type.process_result_value(aware, dialect=None)  # type: ignore[arg-type]
    assert result is not None
    assert result.tzinfo == UTC
    assert result == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
