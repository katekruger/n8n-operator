"""The FastAPI approval application, bound to 127.0.0.1 only.

Not configurable to a public interface in v1 (boundary B10) — enforced upstream by
``config.Settings``' own ``_validate_approval_bind_is_loopback``, so by the time this
module ever sees a ``Settings`` instance, ``approval_bind`` is already guaranteed
loopback. Also hosts the expiry sweeper that writes T08 and T11 (ARCHITECTURE section
8) — best-effort only; lazy transactional expiry (invariant I9) is what actually makes
an ``EXPIRED`` operation unexecutable regardless of whether this process is even
running.

No Swagger/ReDoc/OpenAPI routes are mounted (``docs_url``/``redoc_url``/``openapi_url``
all ``None``): the approval surface is exactly ``/approve/{token}`` and
``/reject/{token}``, and a framework-served docs page is both an unused attack surface
and, for the default Swagger UI, an external-asset dependency this app has no reason to
carry ("no external assets required"). Access logging is disabled entirely
(``access_log=False``) so the approval token embedded in every request path is never
written to a log file.

Phase 6 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import uvicorn
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.approval.routes import ApprovalAppDeps, build_router
from n8n_operator.core import service
from n8n_operator.storage.session import session_scope

__all__ = ["build_app", "run_approval_app"]

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_SWEEP_INTERVAL_SECONDS = 30


async def _sweep_loop(session_factory: sessionmaker[Session]) -> None:
    """Best-effort periodic expiry (ADR-010 section 4: "the sweeper is best-effort;
    maintenance is explicit"). A sweep failure is logged and skipped, never allowed to
    crash the approval app — nothing about safety depends on this loop; only how
    promptly an ``EXPIRED`` audit event appears after the deadline actually passes.
    """
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
        try:
            with session_scope(session_factory) as session:
                service.expire_overdue_operations(session)
        except Exception:
            logger.exception("approval app: best-effort expiry sweep failed")


def build_app(
    approval_bind: str, session_factory: sessionmaker[Session], *, enable_v2: bool = False
) -> FastAPI:
    """Construct the approval app. Does not bind or serve — see :func:`run_approval_app`.

    Takes ``approval_bind`` as a plain ``HOST:PORT`` string, not a full
    :class:`~n8n_operator.config.Settings`: approving and rejecting operations never
    touches n8n, so this app — like the ``operations`` CLI commands it mirrors — has no
    reason to require ``N8N_OPERATOR_N8N_BASE_URL``/``N8N_OPERATOR_N8N_API_KEY`` to be
    configured at all. Callers resolve the bind via
    :func:`~n8n_operator.config.resolve_approval_bind`, which validates loopback on its
    own (boundary B10).
    """
    expected_host = approval_bind
    expected_origin = f"http://{expected_host}"

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        sweep_task = asyncio.create_task(_sweep_loop(session_factory))
        try:
            yield
        finally:
            sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep_task

    app = FastAPI(
        title="n8n Operator Approval",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    deps = ApprovalAppDeps(
        session_factory=session_factory,
        expected_host=expected_host,
        expected_origin=expected_origin,
        enable_v2=enable_v2,
    )
    app.include_router(build_router(deps, templates))
    return app


def run_approval_app(
    approval_bind: str,
    session_factory: sessionmaker[Session],
    *,
    log_level: str = "info",
    enable_v2: bool = False,
) -> None:
    """Bind and serve the approval app on ``approval_bind``. Blocks until stopped.
    Always loopback (boundary B10) — :func:`~n8n_operator.config.resolve_approval_bind`
    already refuses to resolve anything else."""
    host, port_str = approval_bind.rsplit(":", 1)
    app = build_app(approval_bind, session_factory, enable_v2=enable_v2)
    config = uvicorn.Config(
        app,
        host=host,
        port=int(port_str),
        log_level=log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(config)
    anyio.run(server.serve)
