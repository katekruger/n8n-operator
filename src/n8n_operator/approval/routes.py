"""Approval routes.

    GET  /approve/{token}   render the pending operation for a human
    POST /approve/{token}   T06 -> APPROVED
    POST /reject/{token}    T07 -> REJECTED

GET grants nothing. Approval requires a POST from a human session with a CSRF token,
Origin and Host validation, and SameSite=Strict cookies (threats T-08, T-15, T-16).
Tokens are 256-bit, single-use, TTL-bounded, and stored only as sha256 hashes
(AC-21).

The raw token from the URL path is never logged (FastAPI/uvicorn access logging is
disabled entirely for this app — see ``approval/app.py``) and never echoed back in any
response body or header beyond the same URL path the client already has.

Phase 6 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import ApprovalDecisionContext
from n8n_operator.errors import (
    ApprovalNotPendingError,
    ApprovalTokenAlreadyUsedError,
    ApprovalTokenInvalidError,
    InvalidStateTransitionError,
)
from n8n_operator.storage.session import session_scope

__all__ = ["ApprovalAppDeps", "build_router"]

CSRF_COOKIE_NAME = "n8n_operator_csrf"

_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@dataclass(frozen=True)
class ApprovalAppDeps:
    """What every route needs, injected once by the composition root
    (``approval/app.py``) — the same seam ``mcp/tools.py``'s ``ToolDeps`` establishes
    for the identical reason."""

    session_factory: sessionmaker[Session]
    expected_host: str
    expected_origin: str


def _no_store(response: HTMLResponse) -> HTMLResponse:
    for key, value in _NO_STORE_HEADERS.items():
        response.headers[key] = value
    return response


def _verify_host(request: Request, deps: ApprovalAppDeps) -> bool:
    """DNS-rebinding defense (threat T-34): the ``Host`` header a browser sends must
    name this loopback bind, not whatever hostname a malicious page's DNS trickery
    pointed the browser at."""
    return request.headers.get("host") == deps.expected_host


def _verify_origin_for_decision(request: Request, deps: ApprovalAppDeps) -> bool:
    """CSRF/rebinding defense for a state-changing POST: the ``Origin`` header, when a
    browser sends one (every modern browser does, on every POST), must name this
    loopback origin. Missing entirely is treated as a failure here — unlike a plain
    GET, a decision is not idempotent enough to be lenient about a header every real
    browser sends."""
    origin = request.headers.get("origin")
    return origin is not None and origin == deps.expected_origin


def _verify_csrf(cookie_value: str | None, form_value: str) -> bool:
    if not cookie_value:
        return False
    return hmac.compare_digest(cookie_value, form_value)


def _arguments_json(context: ApprovalDecisionContext) -> str:
    return json.dumps(context.arguments, indent=2, sort_keys=True)


def build_router(deps: ApprovalAppDeps, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    def _render_error(request: Request, message: str, *, status_code: int) -> HTMLResponse:
        response = templates.TemplateResponse(
            request,
            "decision.html",
            {"error": message},
            status_code=status_code,
        )
        return _no_store(response)

    def _render_decision(
        request: Request, token: str, context: ApprovalDecisionContext, *, csrf_token: str
    ) -> HTMLResponse:
        response = templates.TemplateResponse(
            request,
            "decision.html",
            {
                "context": context,
                "token": token,
                "csrf_token": csrf_token,
                "arguments_json": _arguments_json(context),
            },
        )
        remaining_seconds = None
        if context.approval_expires_at is not None:
            remaining_seconds = max(
                1, int((context.approval_expires_at - datetime.now(UTC)).total_seconds())
            )
        response.set_cookie(
            CSRF_COOKIE_NAME,
            csrf_token,
            httponly=True,
            samesite="strict",
            secure=False,  # loopback HTTP only in v1 (boundary B10) — no TLS to require
            path="/",
            max_age=remaining_seconds,
        )
        return _no_store(response)

    @router.get("/approve/{token}", response_class=HTMLResponse)
    async def get_approval(request: Request, token: str) -> HTMLResponse:
        if not _verify_host(request, deps):
            return _render_error(
                request, "Request rejected: unexpected Host header.", status_code=400
            )
        try:
            with session_scope(deps.session_factory) as session:
                context = service.resolve_approval_token(session, token=token)
        except ApprovalTokenInvalidError:
            return _render_error(request, "This approval link is not valid.", status_code=404)
        except ApprovalTokenAlreadyUsedError:
            return _render_error(
                request, "This approval link has already been used.", status_code=409
            )
        except ApprovalNotPendingError:
            return _render_error(
                request, "This operation is no longer awaiting approval.", status_code=409
            )
        csrf_token = secrets.token_urlsafe(32)
        return _render_decision(request, token, context, csrf_token=csrf_token)

    async def _decide(
        request: Request, token: str, csrf_token: str, *, decision: str
    ) -> HTMLResponse:
        if not _verify_host(request, deps):
            return _render_error(
                request, "Request rejected: unexpected Host header.", status_code=400
            )
        if not _verify_origin_for_decision(request, deps):
            return _render_error(
                request, "Request rejected: unexpected Origin header.", status_code=403
            )
        if not _verify_csrf(request.cookies.get(CSRF_COOKIE_NAME), csrf_token):
            return _render_error(
                request,
                "This form has expired or was not submitted from this page. "
                "Reload the approval link and try again.",
                status_code=403,
            )

        try:
            with session_scope(deps.session_factory) as session:
                # Re-resolve rather than trusting the form: a token that was valid
                # when the page rendered may have been decided, or expired, since.
                resolved = service.resolve_approval_token(session, token=token)
                fingerprint = _client_fingerprint(request)
                if decision == "approved":
                    operation = service.approve_operation(
                        session,
                        operation_id=resolved.operation_id,
                        decided_by="local",
                        client_fingerprint=fingerprint,
                    )
                else:
                    operation = service.reject_operation(
                        session,
                        operation_id=resolved.operation_id,
                        decided_by="local",
                        client_fingerprint=fingerprint,
                    )
        except (
            ApprovalTokenInvalidError,
            ApprovalTokenAlreadyUsedError,
            ApprovalNotPendingError,
        ) as exc:
            return _render_error(request, exc.message, status_code=409)
        except InvalidStateTransitionError:
            # Lost a race against a concurrent decision on this same operation (the
            # other tab, or the CLI) between our resolve and our write — the operation
            # itself is fine; only this particular POST was too late.
            return _render_error(
                request,
                "This operation was just decided elsewhere. Reload the approval link "
                "to see its current state.",
                status_code=409,
            )

        with session_scope(deps.session_factory) as session:
            context = service.get_approval_decision_context(
                session, operation_id=operation.id, principal_id="local"
            )
        response = templates.TemplateResponse(
            request,
            "decision.html",
            {"context": context, "token": token, "arguments_json": _arguments_json(context)},
        )
        return _no_store(response)

    @router.post("/approve/{token}", response_class=HTMLResponse)
    async def post_approve(
        request: Request, token: str, csrf_token: str = Form(...)
    ) -> HTMLResponse:
        return await _decide(request, token, csrf_token, decision="approved")

    @router.post("/reject/{token}", response_class=HTMLResponse)
    async def post_reject(
        request: Request, token: str, csrf_token: str = Form(...)
    ) -> HTMLResponse:
        return await _decide(request, token, csrf_token, decision="rejected")

    return router


def _client_fingerprint(request: Request) -> str:
    """Coarse request provenance for the audit trail (BUILD_PLAN section 8.1) — never
    the raw token, never anything that could stand in for a credential; just enough
    for a human reading the audit export to tell requests apart."""
    client_host = request.client.host if request.client else "?"
    user_agent = request.headers.get("user-agent", "?")
    return f"{client_host} {user_agent}"[:200]
