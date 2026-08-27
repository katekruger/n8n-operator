"""The Typer application root.

Command groups: ``db`` (phase 1), ``registry`` (phase 2), ``serve stdio``/``serve http``
(phase 5), ``operations`` and ``serve approval`` (phase 6); ``audit`` arrives with the
phase that implements it (BUILD_PLAN section 12). ``app`` is the object
``pyproject.toml``'s ``[project.scripts]`` entry point and ``__main__.py`` both invoke —
there is exactly one Typer application, regardless of entry point.

Phase 1 onward (BUILD_PLAN section 12).
"""

from __future__ import annotations

import typer

from n8n_operator.cli.commands import db as db_commands
from n8n_operator.cli.commands import operations as operations_commands
from n8n_operator.cli.commands import registry as registry_commands
from n8n_operator.cli.commands import serve as serve_commands

app = typer.Typer(
    name="n8n-operator",
    help="A governed MCP control plane for approved n8n workflows.",
    no_args_is_help=True,
)

app.add_typer(db_commands.app, name="db")
app.add_typer(registry_commands.app, name="registry")
app.add_typer(serve_commands.app, name="serve")
app.add_typer(operations_commands.app, name="operations")

__all__ = ["app"]
