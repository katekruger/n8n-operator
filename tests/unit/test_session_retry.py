"""Unit tests for ``run_in_session_with_retry``'s control flow, against a fake session
factory — no real database needed. The real-Postgres deadlock case is covered by
``tests/integration/postgres/test_engine.py`` (marked ``postgres``); this file is pure
logic: how many times does it retry, on what, and does it ever retry something it must
not (a non-transient error, or exhausting its attempt budget).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from n8n_operator.storage.session import run_in_session_with_retry


class _FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _sessions_factory(count: int) -> tuple[Any, list[_FakeSession]]:
    made: list[_FakeSession] = []

    def factory() -> _FakeSession:
        s = _FakeSession()
        made.append(s)
        return s

    return factory, made


def _deadlock_error() -> DBAPIError:
    orig = MagicMock()
    orig.sqlstate = "40P01"
    orig.pgcode = "40P01"
    return DBAPIError("statement", {}, orig)


def _serialization_error() -> DBAPIError:
    orig = MagicMock()
    orig.sqlstate = "40001"
    return DBAPIError("statement", {}, orig)


@pytest.mark.unit
def test_succeeds_on_the_first_attempt_with_no_retry() -> None:
    factory, made = _sessions_factory(1)
    calls = []

    def fn(session: Any) -> str:
        calls.append(session)
        return "ok"

    result = run_in_session_with_retry(factory, fn)
    assert result == "ok"
    assert len(calls) == 1
    assert made[0].committed
    assert not made[0].rolled_back


@pytest.mark.unit
def test_retries_a_deadlock_and_succeeds_on_the_second_attempt() -> None:
    factory, made = _sessions_factory(2)
    attempts = {"n": 0}

    def fn(session: Any) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _deadlock_error()
        return "ok"

    result = run_in_session_with_retry(factory, fn, backoff_seconds=0.001)
    assert result == "ok"
    assert attempts["n"] == 2
    assert made[0].rolled_back
    assert not made[0].committed
    assert made[1].committed


@pytest.mark.unit
def test_retries_a_serialization_failure() -> None:
    factory, _made = _sessions_factory(2)
    attempts = {"n": 0}

    def fn(session: Any) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _serialization_error()
        return "ok"

    assert run_in_session_with_retry(factory, fn, backoff_seconds=0.001) == "ok"
    assert attempts["n"] == 2


@pytest.mark.unit
def test_gives_up_after_max_attempts() -> None:
    factory, made = _sessions_factory(3)
    attempts = {"n": 0}

    def fn(session: Any) -> str:
        attempts["n"] += 1
        raise _deadlock_error()

    with pytest.raises(DBAPIError):
        run_in_session_with_retry(factory, fn, max_attempts=3, backoff_seconds=0.001)
    assert attempts["n"] == 3
    assert all(s.rolled_back for s in made)


@pytest.mark.unit
def test_never_retries_a_non_transient_error() -> None:
    """A constraint violation is data, not contention — retrying it reproduces the
    identical violation, so it must propagate on the very first attempt."""
    factory, made = _sessions_factory(1)
    attempts = {"n": 0}

    def fn(session: Any) -> str:
        attempts["n"] += 1
        raise IntegrityError("statement", {}, Exception("unique violation"))

    with pytest.raises(IntegrityError):
        run_in_session_with_retry(factory, fn, max_attempts=5, backoff_seconds=0.001)
    assert attempts["n"] == 1
    assert made[0].rolled_back


@pytest.mark.unit
def test_never_retries_a_plain_application_exception() -> None:
    factory, made = _sessions_factory(1)
    attempts = {"n": 0}

    def fn(session: Any) -> str:
        attempts["n"] += 1
        raise ValueError("not a database problem at all")

    with pytest.raises(ValueError, match="not a database problem"):
        run_in_session_with_retry(factory, fn, max_attempts=5, backoff_seconds=0.001)
    assert attempts["n"] == 1
    assert made[0].rolled_back


@pytest.mark.unit
def test_sqlite_lock_contention_message_is_recognized_as_retryable() -> None:
    factory, _made = _sessions_factory(2)
    attempts = {"n": 0}

    def fn(session: Any) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            orig = MagicMock()
            orig.sqlstate = None
            orig.pgcode = None
            orig.__str__ = MagicMock(return_value="database is locked")  # type: ignore[method-assign]
            raise DBAPIError("statement", {}, orig)
        return "ok"

    assert run_in_session_with_retry(factory, fn, backoff_seconds=0.001) == "ok"
    assert attempts["n"] == 2


@pytest.mark.unit
def test_every_session_is_closed_regardless_of_outcome() -> None:
    factory, made = _sessions_factory(2)
    attempts = {"n": 0}

    def fn(session: Any) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _deadlock_error()
        return "ok"

    run_in_session_with_retry(factory, fn, backoff_seconds=0.001)
    assert all(s.closed for s in made)
