"""Structured logging, correlation IDs, and secret scrubbing (BUILD_PLAN section 12,
phase 8) — pure logic, no database, no CLI process.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from n8n_operator import logging_setup
from n8n_operator.logging_setup import (
    REDACTED_MARKER,
    bind_correlation_id,
    configure_logging,
    correlation_scope,
    get_correlation_id,
    new_correlation_id,
    register_secret,
)


@pytest.fixture(autouse=True)
def _isolated_logging_state() -> Iterator[None]:
    """Every test gets a clean slate: no secret leaks in from an earlier test's
    ``register_secret`` call (production never clears this list; the test suite must),
    and no correlation ID leaks in from an earlier test's ``bind_correlation_id``
    (which — unlike :func:`correlation_scope` — has no matching unbind)."""
    logging_setup._reset_registered_secrets_for_tests()
    token = logging_setup._correlation_id.set(None)
    yield
    logging_setup._correlation_id.reset(token)
    logging_setup._reset_registered_secrets_for_tests()


def _configured_logger(stream: io.StringIO, *, level: str = "INFO") -> logging.Logger:
    configure_logging(level=level, stream=stream)
    return logging.getLogger(logging_setup.LOGGER_NAME)


def _last_line(stream: io.StringIO) -> dict[str, Any]:
    lines = [line for line in stream.getvalue().splitlines() if line]
    assert lines, "no log line was emitted"
    result: dict[str, Any] = json.loads(lines[-1])
    return result


# --------------------------------------------------------------------------------------
# Correlation IDs
# --------------------------------------------------------------------------------------


def test_new_correlation_id_is_unique() -> None:
    assert new_correlation_id() != new_correlation_id()


def test_get_correlation_id_is_none_when_unbound() -> None:
    assert get_correlation_id() is None


def test_correlation_scope_binds_for_the_block_and_restores_after() -> None:
    assert get_correlation_id() is None
    with correlation_scope("abc-123") as bound:
        assert bound == "abc-123"
        assert get_correlation_id() == "abc-123"
    assert get_correlation_id() is None


def test_correlation_scope_mints_one_when_not_given() -> None:
    with correlation_scope() as bound:
        assert bound
        assert get_correlation_id() == bound


def test_correlation_scope_nests_and_restores_the_outer_value() -> None:
    with correlation_scope("outer"):
        with correlation_scope("inner"):
            assert get_correlation_id() == "inner"
        assert get_correlation_id() == "outer"


def test_bind_correlation_id_has_no_matching_unbind() -> None:
    bind_correlation_id("stays-bound")
    assert get_correlation_id() == "stays-bound"
    # No context manager, no restore — the process-lifetime shape a single CLI
    # invocation needs (module docstring).


# --------------------------------------------------------------------------------------
# Structured JSON logging
# --------------------------------------------------------------------------------------


def test_configure_logging_emits_one_json_line_with_the_fixed_fields() -> None:
    stream = io.StringIO()
    logger = _configured_logger(stream)
    logger.info("hello world")
    record = _last_line(stream)
    assert record["level"] == "INFO"
    assert record["logger"] == logging_setup.LOGGER_NAME
    assert record["message"] == "hello world"
    assert record["correlation_id"] == "-"
    assert "timestamp" in record


def test_configure_logging_stamps_the_bound_correlation_id() -> None:
    stream = io.StringIO()
    logger = _configured_logger(stream)
    with correlation_scope("corr-xyz"):
        logger.info("inside scope")
    record = _last_line(stream)
    assert record["correlation_id"] == "corr-xyz"


def test_configure_logging_is_idempotent_no_duplicate_handlers() -> None:
    stream = io.StringIO()
    _configured_logger(stream)
    _configured_logger(stream)  # reconfigure — must not stack a second handler
    logging.getLogger(logging_setup.LOGGER_NAME).info("once")
    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1


def test_configure_logging_respects_level() -> None:
    stream = io.StringIO()
    logger = _configured_logger(stream, level="WARNING")
    logger.info("should be suppressed")
    assert stream.getvalue() == ""
    logger.warning("should appear")
    record = _last_line(stream)
    assert record["message"] == "should appear"


def test_extra_fields_are_merged_into_the_json_payload() -> None:
    stream = io.StringIO()
    logger = _configured_logger(stream)
    logger.info("with extra", extra={"operation_id": "op_123", "outcome": "denied"})
    record = _last_line(stream)
    assert record["operation_id"] == "op_123"
    assert record["outcome"] == "denied"


def test_child_loggers_propagate_to_the_configured_handler() -> None:
    """``approval/app.py``'s own ``logging.getLogger(__name__)`` — a child of this
    namespace — must reach the same handler without any code there needing to know
    logging was configured (module docstring)."""
    stream = io.StringIO()
    _configured_logger(stream)
    child = logging.getLogger(f"{logging_setup.LOGGER_NAME}.approval.app")
    child.info("from a child logger")
    record = _last_line(stream)
    assert record["message"] == "from a child logger"
    assert record["logger"] == f"{logging_setup.LOGGER_NAME}.approval.app"


# --------------------------------------------------------------------------------------
# Secret scrubbing
# --------------------------------------------------------------------------------------


def test_registered_secret_is_scrubbed_from_the_log_message() -> None:
    stream = io.StringIO()
    logger = _configured_logger(stream)
    register_secret("sk-live-super-secret-token")
    logger.info("dispatching with key sk-live-super-secret-token")
    record = _last_line(stream)
    assert "sk-live-super-secret-token" not in json.dumps(record)
    assert REDACTED_MARKER in record["message"]


def test_secret_registered_after_configure_logging_is_still_scrubbed() -> None:
    """The common real sequence: logging is configured before any secret is known
    (the CLI root callback runs first); the secret is registered once
    ``config.load_settings()`` resolves it, later in the same process."""
    stream = io.StringIO()
    logger = _configured_logger(stream)
    logger.info("before registration: sk-not-yet-known")
    register_secret("sk-now-known")
    logger.info("after registration: sk-now-known")
    record = _last_line(stream)
    assert "sk-now-known" not in json.dumps(record)


def test_unregistered_secret_is_not_touched() -> None:
    stream = io.StringIO()
    logger = _configured_logger(stream)
    logger.info("plain message with no secret")
    record = _last_line(stream)
    assert record["message"] == "plain message with no secret"


def test_register_secret_ignores_none_and_empty_string() -> None:
    stream = io.StringIO()
    logger = _configured_logger(stream)
    register_secret(None)
    register_secret("")
    logger.info("")
    record = _last_line(stream)
    assert record["message"] == ""


def test_register_secret_is_idempotent_for_the_same_value() -> None:
    register_secret("dup-secret")
    register_secret("dup-secret")
    assert logging_setup._known_secrets.count("dup-secret") == 1


# --------------------------------------------------------------------------------------
# Deterministic JSON output — matches the CLI's own ``--json`` commitment.
# --------------------------------------------------------------------------------------


def test_json_output_has_sorted_keys() -> None:
    stream = io.StringIO()
    logger = _configured_logger(stream)
    logger.info("x", extra={"zebra": 1, "apple": 2})
    line = next(line for line in stream.getvalue().splitlines() if line)
    # A sorted-keys JSON object's keys appear in the line in alphabetical order.
    keys_in_order = list(json.loads(line, object_pairs_hook=lambda pairs: [k for k, _ in pairs]))
    assert keys_in_order == sorted(keys_in_order)


def test_repeated_configuration_and_logging_is_byte_identical_apart_from_timestamp() -> None:
    """Two independently configured loggers emitting the same record produce the same
    JSON shape and field values — the only thing that can legitimately differ is the
    wall-clock timestamp."""
    stream_a, stream_b = io.StringIO(), io.StringIO()
    logger_a = _configured_logger(stream_a)
    with correlation_scope("same-id"):
        logger_a.info("same message", extra={"k": "v"})
    record_a = _last_line(stream_a)

    logger_b = _configured_logger(stream_b)
    with correlation_scope("same-id"):
        logger_b.info("same message", extra={"k": "v"})
    record_b = _last_line(stream_b)

    del record_a["timestamp"]
    del record_b["timestamp"]
    assert record_a == record_b
