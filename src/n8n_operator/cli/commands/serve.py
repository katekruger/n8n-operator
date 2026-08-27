"""``n8n-operator serve`` — stdio, http, approval.

``serve stdio`` and ``serve http`` need the full :class:`~n8n_operator.config.Settings`
(the n8n credentials, unlike ``db``/``registry``, which deliberately resolve their one
setting each without requiring the rest — see those modules' docstrings): an MCP server
with no n8n instance configured cannot preflight or dispatch anything, so failing at
startup here is correct, not a gap. ``serve approval`` arrives with phase 6.

Phases 5 and 6 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import typer

from n8n_operator.config import load_settings
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
