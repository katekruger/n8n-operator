"""``n8n-operator health`` — ``get_instance_health``, from the command line.

Needs the full n8n configuration (unlike ``db``/``registry``/``operations``/``audit``,
which deliberately resolve only what they need — see those modules' docstrings):
reachability is a property of the configured n8n instance, and nothing here can report
on it without knowing where that instance is and how to authenticate to it.

``_CliHealthAdapter`` converts ``n8n.health.N8nHealth``'s locally-defined, duck-typed
result into ``core.models.HealthCheckResult`` — the same real-type-behind-a-port
conversion ``mcp/server.py``'s own ``_HealthAdapter`` performs for the MCP transport,
duplicated here in miniature rather than imported: this command is its own composition
root (like ``cli/commands/registry.py`` talking to ``registry/loader.py`` directly), and
the conversion is small enough that a shared private class across two files would cost
more in coupling than it saves in lines.

The result carries no URL and no credential (MCP_TOOLS.md section 2.3, boundary B5) — a
discovery tool, not a way to learn where the instance lives — so nothing printed here
needs redaction; the shape itself cannot leak one.

Phase 8 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import json

import typer

from n8n_operator.config import load_settings
from n8n_operator.core import service
from n8n_operator.core.models import HealthCheckResult
from n8n_operator.logging_setup import configure_logging, register_secret
from n8n_operator.n8n.client import N8nClient
from n8n_operator.n8n.health import N8nHealth


class _CliHealthAdapter:
    def __init__(self, impl: N8nHealth) -> None:
        self._impl = impl

    def check(self) -> HealthCheckResult:
        raw = self._impl.check()
        return HealthCheckResult(
            reachable=raw.reachable,
            n8n_version=raw.n8n_version,
            latency_ms=raw.latency_ms,
            reason=raw.reason,
            checked_at=raw.checked_at,
        )


def health(
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Check whether the configured n8n instance is reachable."""
    settings = load_settings()
    configure_logging(level=settings.log_level)
    register_secret(settings.n8n_api_key.get_secret_value())

    client = N8nClient(
        base_url=str(settings.n8n_base_url),
        api_key=settings.n8n_api_key.get_secret_value(),
        connect_timeout_seconds=float(settings.request_timeout_seconds),
    )
    result = service.get_instance_health(_CliHealthAdapter(N8nHealth(client)))

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "reachable": result.reachable,
                    "n8n_version": result.n8n_version,
                    "latency_ms": result.latency_ms,
                    "reason": result.reason,
                    "checked_at": result.checked_at.isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"reachable:    {result.reachable}")
        if result.n8n_version is not None:
            typer.echo(f"n8n_version:  {result.n8n_version}")
        if result.latency_ms is not None:
            typer.echo(f"latency_ms:   {result.latency_ms}")
        if result.reason is not None:
            typer.echo(f"reason:       {result.reason}")
        typer.echo(f"checked_at:   {result.checked_at.isoformat()}")

    if not result.reachable:
        raise typer.Exit(code=1)


__all__ = ["health"]
