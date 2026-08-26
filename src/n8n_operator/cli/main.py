"""The Typer application root.

Command groups: ``db`` (phase 1), ``registry`` (phase 2); ``serve``, ``operations``,
``audit`` arrive with the phases that implement them (BUILD_PLAN section 12). ``app`` is
the object ``pyproject.toml``'s ``[project.scripts]`` entry point and ``__main__.py``
both invoke — there is exactly one Typer application, regardless of entry point.

Phase 1 onward (BUILD_PLAN section 12).
"""

from __future__ import annotations

import typer

from n8n_operator.cli.commands import db as db_commands
from n8n_operator.cli.commands import registry as registry_commands

app = typer.Typer(
    name="n8n-operator",
    help="A governed MCP control plane for approved n8n workflows.",
    no_args_is_help=True,
)

app.add_typer(db_commands.app, name="db")
app.add_typer(registry_commands.app, name="registry")

__all__ = ["app"]
