"""The Typer application root.

Command groups: ``db`` (phase 1), ``registry`` (phase 2), ``serve stdio``/``serve http``
(phase 5), ``operations`` and ``serve approval`` (phase 6), ``audit`` and ``health``
(phase 8), ``identity`` (phase 10, v2 stage 02). ``app`` is the object
``pyproject.toml``'s ``[project.scripts]`` entry point
and ``__main__.py`` both invoke — there is exactly one Typer application, regardless of
entry point.

The root callback below runs before every subcommand: it configures structured JSON
logging (``logging_setup.configure_logging``) and binds one correlation ID for the
whole invocation, so every log line any subcommand emits — including ones from
``approval/app.py``'s own logger, which propagates up to the same namespace — carries
it. ``--log-level`` defaults to ``INFO``, matching ``config.Settings.log_level``'s own
default and sharing its ``N8N_OPERATOR_LOG_LEVEL`` env var name — but this callback
reads that var directly (Typer's ``envvar=``), not through ``Settings``, since most
commands (``db``/``registry``/``operations``/``audit``) deliberately never construct
one. A command that *does* load ``Settings`` (``serve stdio``/``serve http``,
``health``) reconfigures logging again with ``settings.log_level`` once it's resolved
— picking up a value set only in a ``.env`` file, which this callback's plain envvar
read cannot see — and registers the n8n credential(s) it just learned with
``logging_setup.register_secret``, upgrading scrubbing coverage for the rest of the
invocation.

Phase 1 onward (BUILD_PLAN section 12).
"""

from __future__ import annotations

import typer

from n8n_operator.cli.commands import audit as audit_commands
from n8n_operator.cli.commands import db as db_commands
from n8n_operator.cli.commands import environment as environment_commands
from n8n_operator.cli.commands import health as health_commands
from n8n_operator.cli.commands import identity as identity_commands
from n8n_operator.cli.commands import operations as operations_commands
from n8n_operator.cli.commands import registry as registry_commands
from n8n_operator.cli.commands import serve as serve_commands
from n8n_operator.logging_setup import bind_correlation_id, configure_logging

app = typer.Typer(
    name="n8n-operator",
    help="A governed MCP control plane for approved n8n workflows.",
    no_args_is_help=True,
)


@app.callback()
def main(
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        envvar="N8N_OPERATOR_LOG_LEVEL",
        help="Level for structured operational logs (not this command's own output).",
    ),
) -> None:
    """A governed MCP control plane for approved n8n workflows."""
    configure_logging(level=log_level)
    bind_correlation_id()


app.add_typer(db_commands.app, name="db")
app.add_typer(registry_commands.app, name="registry")
app.add_typer(serve_commands.app, name="serve")
app.add_typer(operations_commands.app, name="operations")
app.add_typer(audit_commands.app, name="audit")
app.add_typer(identity_commands.app, name="identity")
app.add_typer(environment_commands.app, name="environment")
app.command("health")(health_commands.health)

__all__ = ["app"]
