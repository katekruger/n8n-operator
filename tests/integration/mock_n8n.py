"""A mock n8n HTTP transport for integration tests (BUILD_PLAN section 12, phase 4).

Builds an ``httpx.MockTransport`` that simulates n8n's control-plane endpoints
(``/healthz``, ``/api/v1/workflows/{id}``, ``/api/v1/executions``) and webhook
dispatch (``/webhook/{path}``) without a real network call, so integration tests can
exercise ``n8n/client.py``, ``n8n/preflight.py``, and (in later phases) full
prepare/execute lifecycles against predictable, injectable responses — the same shape
of fixture BUILD_PLAN section 10.1 calls for: "real SQLite + Alembic + a mock n8n
served by ``httpx.MockTransport``".

Not a fake worth calling "realistic" beyond what these tests need: it does not run
workflows, evaluate node logic, or enforce n8n's own validation. It returns exactly what
each test configures, plus request bookkeeping (``self.requests``) so a test can assert
things like "dispatched exactly once".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx

__all__ = ["MockN8n"]

WebhookHandler = Callable[[httpx.Request], httpx.Response]


class MockN8n:
    """Configure, then call :meth:`transport` to get an ``httpx.MockTransport`` to
    pass as ``N8nClient(..., transport=mock.transport())``."""

    def __init__(self) -> None:
        self.healthy: bool = True
        self.api_version: str = "1.1.1"
        self.workflows: dict[str, dict[str, Any]] = {}
        self.executions: dict[str, dict[str, Any]] = {}
        self.webhook_handlers: dict[str, WebhookHandler] = {}
        self.requests: list[httpx.Request] = []
        self.unreachable: bool = False
        self.timeout: bool = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # ------------------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------------------

    def add_workflow(self, n8n_workflow_id: str, definition: dict[str, Any]) -> None:
        self.workflows[n8n_workflow_id] = definition

    def add_execution(self, execution_id: str, record: dict[str, Any]) -> None:
        self.executions[execution_id] = record

    def add_webhook_response(
        self, path: str, *, status: int = 200, body: dict[str, Any] | None = None
    ) -> None:
        def handler(
            _request: httpx.Request, status: int = status, body: Any = body
        ) -> httpx.Response:
            return httpx.Response(status, json=body if body is not None else {})

        self.webhook_handlers[path] = handler

    def add_webhook_timeout(self, path: str) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("mock: read timeout", request=request)

        self.webhook_handlers[path] = handler

    def add_webhook_connection_error(self, path: str) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("mock: connection refused", request=request)

        self.webhook_handlers[path] = handler

    def add_webhook_malformed(
        self, path: str, *, status: int = 200, raw_body: str = "not json"
    ) -> None:
        def handler(
            _request: httpx.Request, status: int = status, raw_body: str = raw_body
        ) -> httpx.Response:
            return httpx.Response(status, content=raw_body.encode("utf-8"))

        self.webhook_handlers[path] = handler

    # ------------------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        if self.unreachable:
            raise httpx.ConnectError("mock: instance unreachable", request=request)
        if self.timeout:
            raise httpx.ReadTimeout("mock: read timeout", request=request)

        path = request.url.path

        if path == "/healthz":
            if self.healthy:
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(503, json={"status": "error"})

        if path == "/api/v1/openapi.yml":
            return httpx.Response(
                200, text=f"openapi: 3.0.0\ninfo:\n  version: {self.api_version}\n"
            )

        if path.startswith("/api/v1/workflows/"):
            workflow_id = path.removeprefix("/api/v1/workflows/")
            definition = self.workflows.get(workflow_id)
            if definition is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=definition)

        if path == "/api/v1/executions":
            query = parse_qs(request.url.query.decode())
            workflow_ids = query.get("workflowId")
            filter_workflow_id = workflow_ids[0] if workflow_ids else None
            items = [
                record
                for record in self.executions.values()
                if filter_workflow_id is None or record.get("workflowId") == filter_workflow_id
            ]
            return httpx.Response(200, json={"data": items, "nextCursor": None})

        if path.startswith("/api/v1/executions/"):
            execution_id = path.removeprefix("/api/v1/executions/")
            record = self.executions.get(execution_id)
            if record is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=record)

        if path.startswith("/webhook/") or path.startswith("/webhook-test/"):
            handler = self.webhook_handlers.get(path)
            if handler is not None:
                return handler(request)
            return httpx.Response(404, json={"message": "webhook not registered"})

        return httpx.Response(404, json={"message": "Not Found"})
