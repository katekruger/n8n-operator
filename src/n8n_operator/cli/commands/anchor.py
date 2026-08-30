"""``n8n-operator anchor`` — external audit anchoring (stage 09, ADR-012 section 2).

No in-process scheduler exists anywhere in this codebase (``operations expire``,
``notifications retry-failed``/``check-alerts`` are all "idempotent CLI command,
operator wires it to cron/systemd") — ``anchor publish`` follows the identical shape.

Admin-gated in v2 mode, the same as ``audit verify``/``audit export`` (anchoring is a
system-wide, cross-principal administrative action, not workflow-scoped).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import typer
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.audit_anchor.keys import (
    KeyFileExistsError,
    generate_keypair,
    load_private_key,
    public_key_b64,
    save_private_key,
)
from n8n_operator.audit_anchor.local_file import LocalFileAnchor
from n8n_operator.audit_anchor.webhook import HttpsWebhookAnchor
from n8n_operator.config import (
    resolve_anchor_config,
    resolve_database_url,
    resolve_v2_identity_flags,
)
from n8n_operator.core import service
from n8n_operator.core.identity import resolve_cli_principal_id
from n8n_operator.core.models import AnchorReceipt, AnchorVerification, ChainAnchor
from n8n_operator.errors import InsufficientRoleError, OperatorError
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

app = typer.Typer(help="External audit anchoring (ADR-012 section 2).", no_args_is_help=True)

EXIT_CHAIN_BROKEN = 2
"""Mirrors ``audit.py``'s own constant — distinct from ``1`` (a general/usage error)
so a monitoring script can tell "verification failed" apart from "invoked wrong"."""


@contextmanager
def _connected() -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine_for_url(resolve_database_url())
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def _database_not_initialized_or_exit() -> None:
    typer.secho(
        "Database is not initialized — run `n8n-operator db init` first.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=1)


def _insufficient_role_or_exit() -> None:
    typer.secho("This command requires the admin role.", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _resolve_principal(session: Session) -> tuple[str, bool]:
    enable_v2, dev_principal_id = resolve_v2_identity_flags()
    principal_id = resolve_cli_principal_id(
        session, enable_v2=enable_v2, dev_principal_id=dev_principal_id
    )
    return principal_id, enable_v2


def _build_sink(
    implementation: str,
) -> tuple[LocalFileAnchor | HttpsWebhookAnchor, str]:
    """Resolves config and constructs the corresponding concrete sink, returning
    ``(sink, public_key_b64)`` — the public key an auditor needs, regardless of which
    implementation is in play."""
    cfg = resolve_anchor_config(implementation)
    if not cfg.signing_key_path.exists():
        typer.secho(
            f"No signing key at {cfg.signing_key_path} — run `n8n-operator anchor init-key` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    private_key = load_private_key(cfg.signing_key_path)
    public_key = public_key_b64(private_key)
    if cfg.implementation == "local_file":
        anchor_file = cfg.signing_key_path.with_name(cfg.signing_key_path.name + ".anchors.jsonl")
        return LocalFileAnchor(path=anchor_file, private_key=private_key), public_key
    assert cfg.webhook_url is not None and cfg.webhook_bearer_token is not None
    return (
        HttpsWebhookAnchor(
            url=cfg.webhook_url, bearer_token=cfg.webhook_bearer_token, private_key=private_key
        ),
        public_key,
    )


class _ServiceSinkAdapter:
    """Converts the concrete sink's own local ``AnchorReceipt``/``AnchorVerification``
    dataclasses into ``core.models``'s Pydantic equivalents — this command is its own
    composition root, like ``cli/commands/notifications.py``'s adapter of the same
    shape."""

    def __init__(self, impl: LocalFileAnchor | HttpsWebhookAnchor) -> None:
        self._impl = impl

    def publish(self, anchor: ChainAnchor) -> AnchorReceipt:
        raw = self._impl.publish(anchor)
        return AnchorReceipt(
            implementation=raw.implementation,  # type: ignore[arg-type]
            detail=raw.detail,
            signature=raw.signature,
            public_key=raw.public_key,
        )

    def verify(self, anchor: ChainAnchor, receipt: AnchorReceipt) -> AnchorVerification:
        from n8n_operator.audit_anchor.local_file import AnchorReceipt as RawReceipt

        raw_receipt = RawReceipt(
            implementation=receipt.implementation,
            detail=receipt.detail,
            signature=receipt.signature,
            public_key=receipt.public_key,
        )
        raw = self._impl.verify(anchor, raw_receipt)
        return AnchorVerification(
            ok=raw.ok, reason=raw.reason, checked_through_seq=raw.checked_through_seq
        )


@app.command("init-key")
def init_key(
    path: Path | None = typer.Option(
        None, "--path", help="Where to write the private key (default: configured path)."
    ),
) -> None:
    """Generate an Ed25519 keypair and write the private key to a file with ``0600``
    permissions. Refuses to overwrite an existing key file — rotating means moving or
    removing the old one first, a deliberate action, not a default."""
    key_path = path or resolve_anchor_config().signing_key_path
    private_bytes, _public_bytes = generate_keypair()
    try:
        save_private_key(key_path, private_bytes)
    except KeyFileExistsError:
        typer.secho(
            f"A key already exists at {key_path}. Refusing to overwrite.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None
    private_key = load_private_key(key_path)
    typer.secho(f"Signing key written to {key_path} (0600).", fg=typer.colors.GREEN)
    typer.echo(f"public_key: {public_key_b64(private_key)}")


@app.command("publish")
def publish(
    implementation: str | None = typer.Option(
        None, "--implementation", help="local_file or https_webhook (default: configured)."
    ),
) -> None:
    """Publish the current chain tip as a new anchor. Idempotent — nothing new to
    anchor exits 0 with a message, not an error. A failed publish is visible here
    (exit 1), in the ``audit_anchors`` row (``publish_failed=True``), and in
    structured logs — three independent channels."""
    resolved_implementation = implementation or resolve_anchor_config().implementation
    sink_impl, _public_key = _build_sink(resolved_implementation)
    sink = _ServiceSinkAdapter(sink_impl)

    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                principal_id, enable_v2 = _resolve_principal(session)
                result = service.publish_anchor(
                    session,
                    sink=sink,
                    implementation=resolved_implementation,
                    principal_id=principal_id,
                    enable_v2=enable_v2,
                )
        except OperationalError:
            _database_not_initialized_or_exit()
            return
        except InsufficientRoleError:
            _insufficient_role_or_exit()
            return
        except OperatorError as exc:
            typer.secho(exc.message, fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None

    if result is None:
        typer.echo("Nothing to anchor yet (the audit log is empty).")
        return
    if result.publish_failed:
        typer.secho(
            f"Publish failed for covers_through_seq={result.covers_through_seq}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    typer.secho(
        f"Anchored through seq={result.covers_through_seq} (entry_hash={result.entry_hash}).",
        fg=typer.colors.GREEN,
    )


@app.command("verify")
def verify(
    implementation: str | None = typer.Option(
        None, "--implementation", help="local_file or https_webhook (default: configured)."
    ),
    database_url: str | None = typer.Option(
        None, "--database-url", help="Verify against this independent database copy instead."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Verify the latest published anchor. Without ``--database-url``, this is a
    self-check against the operator's own live database (like ``audit verify``
    today); with it, verifies against a genuinely independent copy, mutating neither
    side."""
    resolved_implementation = implementation or resolve_anchor_config().implementation
    sink_impl, public_key = _build_sink(resolved_implementation)
    sink = _ServiceSinkAdapter(sink_impl)

    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                principal_id, enable_v2 = _resolve_principal(session)
                latest = service.get_latest_anchor(
                    session,
                    implementation=resolved_implementation,
                    principal_id=principal_id,
                    enable_v2=enable_v2,
                )
        except OperationalError:
            _database_not_initialized_or_exit()
            return
        except InsufficientRoleError:
            _insufficient_role_or_exit()
            return

    if latest is None:
        typer.secho(
            "No anchor has been published yet for this implementation.", fg=typer.colors.YELLOW
        )
        raise typer.Exit(code=1)

    # `anchored_at` was part of what got signed — reconstruct it from the stored
    # receipt, never from `published_at` (a different timestamp, recorded a moment
    # later, that was never part of the signature).
    anchored_at_raw = latest.receipt.get("anchored_at")
    anchored_at = (
        datetime.fromisoformat(anchored_at_raw) if anchored_at_raw else latest.published_at
    )
    anchor = ChainAnchor(
        covers_through_seq=latest.covers_through_seq,
        entry_hash=latest.entry_hash,
        entry_count=latest.covers_through_seq,
        anchored_at=anchored_at,
    )
    receipt = AnchorReceipt(
        implementation=latest.implementation,  # type: ignore[arg-type]
        detail=latest.receipt.get("detail", {}),
        signature=latest.receipt.get("signature", ""),
        public_key=latest.receipt.get("public_key", public_key),
    )

    if database_url is None:
        result = sink.verify(anchor, receipt)
    else:
        result = service.verify_anchor_against_database(
            database_url=database_url,
            covers_through_seq=latest.covers_through_seq,
            entry_hash=latest.entry_hash,
            signature=receipt.signature,
            public_key=receipt.public_key,
        )

    if as_json:
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    elif result.ok:
        typer.secho(
            f"OK — verified through seq={result.checked_through_seq}.", fg=typer.colors.GREEN
        )
    else:
        typer.secho(f"FAILED: {result.reason}", fg=typer.colors.RED, err=True)

    if not result.ok:
        raise typer.Exit(code=EXIT_CHAIN_BROKEN)


@app.command("status")
def status(
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """One line per implementation that has ever published: last covered sequence,
    whether the live chain has grown since, whether the last attempt failed."""
    with _connected() as session_factory:
        try:
            with session_scope(session_factory) as session:
                principal_id, enable_v2 = _resolve_principal(session)
                summaries = service.get_anchor_status(
                    session, principal_id=principal_id, enable_v2=enable_v2
                )
        except OperationalError:
            _database_not_initialized_or_exit()
            return
        except InsufficientRoleError:
            _insufficient_role_or_exit()
            return

    if as_json:
        typer.echo(
            json.dumps([s.model_dump(mode="json") for s in summaries], indent=2, sort_keys=True)
        )
    elif not summaries:
        typer.echo("(no anchors published yet)")
    else:
        for s in summaries:
            state = "FAILED" if s.last_publish_failed else "ok"
            typer.echo(
                f"{s.implementation}: covers_through_seq={s.last_covers_through_seq} "
                f"chain_tip_seq={s.chain_tip_seq} entries_since_last_anchor="
                f"{s.entries_since_last_anchor} last_attempt={state}"
            )

    if any(s.last_publish_failed for s in summaries):
        raise typer.Exit(code=1)


__all__ = ["app"]
