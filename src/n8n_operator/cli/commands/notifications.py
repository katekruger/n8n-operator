"""``n8n-operator notifications`` — bounded retry over undelivered notifications
(ADR-018; stage 05).

Like ``operations expire``, a maintenance convenience for a schedule (cron, a systemd
timer): delivery is attempted inline wherever it originates (``request_approval``), so
this command exists only to give a `pending` row — one whose sink attempt failed and
was not yet retried — another chance, up to ``max_attempts`` before it becomes
permanently `failed` (fail-visible, never retried again). Never touches n8n, so it
resolves only ``database_url`` and the notification sink configuration.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import typer
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.config import resolve_database_url, resolve_notification_sink_config
from n8n_operator.core import service
from n8n_operator.core.models import DeliveryOutcome, NotificationEvent
from n8n_operator.notifications.local import LocalNotificationSink
from n8n_operator.notifications.webhook import WebhookNotificationSink
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

app = typer.Typer(help="Retry failed notification delivery.", no_args_is_help=True)


class _CliNotificationSinkAdapter:
    """Converts a ``notifications/`` package sink's local ``DeliveryOutcome`` into
    ``core.models.DeliveryOutcome`` — as ``cli/commands/operations.py``'s adapter of
    the same name does, duplicated here rather than imported (this command is its own
    composition root, like ``cli/commands/health.py``'s ``_CliHealthAdapter``)."""

    def __init__(self, impl: LocalNotificationSink | WebhookNotificationSink) -> None:
        self._impl = impl

    def deliver(self, event: NotificationEvent) -> DeliveryOutcome:
        raw = self._impl.deliver(event)
        return DeliveryOutcome(delivered=raw.delivered, detail=raw.detail)


def _cli_notification_sink() -> _CliNotificationSinkAdapter:
    sink, url, token = resolve_notification_sink_config()
    if sink == "webhook":
        assert url is not None and token is not None
        return _CliNotificationSinkAdapter(WebhookNotificationSink(url=url, bearer_token=token))
    return _CliNotificationSinkAdapter(LocalNotificationSink())


@contextmanager
def _connected() -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine_for_url(resolve_database_url())
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


@app.command("retry-failed")
def retry_failed() -> None:
    """Re-attempt every ``pending`` notification delivery now, across every
    operation. Idempotent: a delivery already recorded ``delivered`` is never
    re-attempted (``core.service._deliver_with_dedup``'s own dedup), and running this
    twice in a row, or concurrently with another sweep, only ever advances each row's
    own attempt count."""
    try:
        sink = _cli_notification_sink()
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                count = service.retry_failed_notifications(session, sink=sink)
        except OperationalError:
            typer.secho(
                "Database is not initialized — run `n8n-operator db init` first.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None
    typer.echo(f"Retried {count} notification(s).")


@app.command("check-alerts")
def check_alerts(
    executing_stuck_threshold_seconds: int = typer.Option(
        3600,
        "--executing-stuck-threshold-seconds",
        help="How long an EXECUTING operation must sit unchanged before it alerts.",
    ),
) -> None:
    """Sweep for the two alert-hook conditions that need periodic detection (stage 08,
    BUILD_PLAN section 8): an ``EXECUTING`` operation stuck past a threshold, and an
    operation that has reached ``UNKNOWN``. A maintenance convenience for a cron/systemd
    timer, like ``retry-failed`` — idempotent, safe to run on any schedule
    (``core.service.check_and_deliver_alerts``'s own permanent per-event dedup means a
    re-run never re-alerts on something already delivered).

    Drift alerts are reactive, already fired inline by ``prepare_operation``/
    ``retry_operation`` the moment they discover it — this command covers only the two
    swept triggers, never drift.
    """
    try:
        sink = _cli_notification_sink()
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                count = service.check_and_deliver_alerts(
                    session,
                    sink=sink,
                    executing_stuck_threshold_seconds=executing_stuck_threshold_seconds,
                )
        except OperationalError:
            typer.secho(
                "Database is not initialized — run `n8n-operator db init` first.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None
    typer.echo(f"Delivered {count} alert(s).")


__all__ = ["app"]
