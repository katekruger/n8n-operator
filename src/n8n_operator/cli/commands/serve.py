"""``n8n-operator serve`` — stdio, http, approval.

``serve stdio`` and ``serve http`` need the full :class:`~n8n_operator.config.Settings`
(the n8n credentials, unlike ``db``/``registry``, which deliberately resolve their one
setting each without requiring the rest — see those modules' docstrings): an MCP server
with no n8n instance configured cannot preflight or dispatch anything, so failing at
startup here is correct, not a gap.

``serve approval`` is the opposite case, like ``operations approve``/``reject``/
``expire`` (``cli/commands/operations.py``): approving and rejecting an operation never
touches n8n, so it resolves only ``database_url`` and ``approval_bind`` — via
:func:`~n8n_operator.config.resolve_database_url` and
:func:`~n8n_operator.config.resolve_approval_bind` — and never requires
``N8N_OPERATOR_N8N_BASE_URL``/``N8N_OPERATOR_N8N_API_KEY`` to be set at all.

Phases 5 and 6 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import typer

from n8n_operator.approval.app import run_approval_app
from n8n_operator.config import load_settings, resolve_approval_bind, resolve_database_url
from n8n_operator.mcp.transports import serve_http, serve_stdio
from n8n_operator.storage.session import create_engine_for_url, create_session_factory

app = typer.Typer(help="Run the MCP server or the approval app.", no_args_is_help=True)


@app.command("stdio")
def stdio() -> None:
    """Run the MCP server over stdio — the default transport for Claude Desktop and
    any host that launches this process as a subprocess. Blocks until the client
    disconnects."""
    settings = load_settings()
    engine = create_engine_for_url(settings.database_url)
    session_factory = create_session_factory(engine)
    serve_stdio(settings, session_factory)


@app.command("http")
def http() -> None:
    """Run the MCP server over Streamable HTTP, bound to ``settings.http_bind``
    (``127.0.0.1:8000`` by default). A non-loopback bind requires
    ``N8N_OPERATOR_HTTP_BEARER_TOKEN`` and ``N8N_OPERATOR_HTTP_ALLOWED_ORIGINS`` to already
    be set — ``load_settings`` refuses to start otherwise (boundary B9)."""
    settings = load_settings()
    engine = create_engine_for_url(settings.database_url)
    session_factory = create_session_factory(engine)
    serve_http(settings, session_factory)


@app.command("approval")
def approval() -> None:
    """Run the loopback approval web app, bound to ``N8N_OPERATOR_APPROVAL_BIND``
    (``127.0.0.1:8765`` by default) — a convenience alternative to
    ``n8n-operator operations approve``/``reject``, never the only way to decide an
    operation (ADR-010). Always loopback; there is no non-loopback mode to opt into
    (boundary B10)."""
    database_url = resolve_database_url()
    approval_bind = resolve_approval_bind()
    engine = create_engine_for_url(database_url)
    session_factory = create_session_factory(engine)
    run_approval_app(approval_bind, session_factory)
