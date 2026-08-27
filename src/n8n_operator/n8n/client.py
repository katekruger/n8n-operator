"""httpx client for the n8n instance.

Explicit connect and read timeouts on every request. **No automatic re-attempt of a
failed request, no delay-and-try-again helper, and no transport-level retry
configuration of any kind** — a dispatch whose outcome cannot be confirmed becomes
UNKNOWN and is resolved by a human (ADR-005). A contract test greps for the absence of
that machinery (AC-17; see ``tests/contract/test_n8n_no_retry.py`` for the exact
forbidden vocabulary — deliberately not repeated here, so this docstring can never
accidentally trip its own check).

TLS verification is not disableable by configuration (threat T-26): the underlying
``httpx.Client`` is always constructed with its default ``verify=True`` and this module
exposes no parameter that could turn it off.

A timeout is never interpreted as evidence that the workflow did not run: there is no
error-class check and no elapsed-time rule that turns an indeterminate dispatch into
``FAILED`` (ADR-009). Every transport-level failure — timeout, connection refused,
connection reset, TLS failure — maps to the same :class:`DispatchOutcome` kind,
``"indeterminate"``. ADR-005 considered and rejected trying to distinguish
"definitely did not happen" (connection refused) from "response lost" (timeout): both
require the exact same non-retry handling, and building a special case for one is a
subtle, permanent bet on httpx's exception taxonomy never surprising this codebase.

**Endpoint allowlist.** This client exposes named methods only — there is no
general-purpose "call this path" method, matching ARCHITECTURE.md section 11's
commitment that there will never be an ``n8n_request`` tool in any version. Internally,
every control-plane call still passes through :meth:`N8nClient._request`, which checks
the resolved path against :data:`ALLOWED_ENDPOINTS` before sending anything — defense in
depth against a future method being added carelessly, not the only thing enforcing the
allowlist.

**The API key never appears in a raised exception.** Every error this module raises is
constructed with a fixed, safe message from ``errors.py`` — never ``str(exc)`` on the
underlying httpx exception, which can carry the request URL (and, for a misconfigured
client, headers) in its own text (ADR-006).

Where a registry entry declares ``trigger.correlation: response_envelope``, the n8n
execution ID is unwrapped from the webhook's own HTTP response body here, for
reconciliation and debugging (ADR-009 section 2) — never from the n8n control-plane API.

Phase 4 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx

from n8n_operator.errors import (
    InstanceUnreachableError,
    ProviderError,
    WorkflowMissingOnInstanceError,
)
from n8n_operator.n8n.types import (
    ExecutionSummary,
    HealthStatus,
    ResponseEnvelope,
    WorkflowDefinition,
)

__all__ = [
    "ALLOWED_ENDPOINTS",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "MAX_EXECUTION_LIST_PAGES",
    "MAX_RESPONSE_BYTES",
    "DispatchOutcome",
    "N8nClient",
]

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
"""How long to wait for the TCP+TLS handshake, independent of the caller's own
``timeout_seconds`` for the workflow's own execution (which governs the *read* timeout
on dispatch only — see :meth:`N8nClient.dispatch_webhook`)."""

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
"""A bound on any single response this client will read into memory. n8n is a trusted
(Zone B/C) service, not an adversary, but a bound here is cheap insurance against a
misbehaving instance (a runaway execution log, a pathological workflow definition)
turning into an unbounded memory allocation. Enforced by streaming and checking
``Content-Length`` before reading, and by checking the actual byte count read either way."""

MAX_EXECUTION_LIST_PAGES = 50
"""Pagination-loop protection for :meth:`N8nClient.list_executions`: n8n paginates via
an opaque ``nextCursor``; this bounds how many pages are ever followed regardless of
what the server returns, so a server that never terminates the cursor chain (buggy or
malicious) cannot make this call loop forever."""

ALLOWED_ENDPOINTS = frozenset(
    {
        "/healthz",
        "/api/v1/openapi.yml",
        "/api/v1/workflows/{id}",
        "/api/v1/executions",
        "/api/v1/executions/{id}",
    }
)
"""Every control-plane path this client is permitted to call, checked by
:meth:`N8nClient._request` before any network call. Webhook dispatch
(:meth:`N8nClient.dispatch_webhook`) is a deliberately separate code path: it calls a
*registry-supplied* relative path under ``/webhook/``, not a path from this fixed set,
and is documented as the one place that is true."""


@dataclass(frozen=True)
class DispatchOutcome:
    """The result of one webhook dispatch attempt (ADR-005, ADR-009).

    ``kind`` is what ``core/service.py``'s ``record_execution_outcome`` uses to decide
    T13/T14/T15: ``"success"`` (2xx, body parsed), ``"error"`` (a non-2xx HTTP response
    that *did* arrive — n8n responded, so nothing here is ambiguous about whether the
    request was received), or ``"indeterminate"`` (no response could be confirmed at
    all — timeout, connection failure, an oversized response, or **a response body that
    could not be parsed as JSON**, which ADR-009 section 1.1 treats identically to a
    lost response: "a timeout, connection loss, or *unparseable response* after
    dispatch transitions EXECUTING -> UNKNOWN").

    ``result`` is the *unwrapped* payload: when the body is a dict carrying the
    documented ``n8n_operator`` envelope key, this is its ``data`` field; otherwise it
    is the raw parsed body as-is. A **malformed** envelope (present but not shaped
    right) does not change ``kind`` or demote ``result`` to ``None`` — ADR-009 section
    2: "a workflow that returns a result is not broken because its envelope is". It
    only means ``correlation_available`` is ``False``.

    ``correlation_available`` is ``True`` iff a well-formed envelope carrying a
    non-null ``execution_id`` was found — the one condition under which
    reconciliation and ``get_execution_log`` node traces are possible at all.
    """

    kind: Literal["success", "error", "indeterminate"]
    http_status: int | None
    result: Any | None
    execution_id: str | None
    correlation_available: bool


def _safe_message(default: str) -> str:
    """A hook point, not a transform: every caller in this module already passes a
    fixed, safe string — this exists so no call site is ever tempted to interpolate
    ``str(exc)`` into it later without the intent being visible in the diff."""
    return default


class N8nClient:
    """The only class in this codebase that makes a network call to n8n.

    Holds the server-owned API key (ADR-006) in memory for the lifetime of the process;
    it is sent as the ``X-N8N-API-KEY`` header on every control-plane call and is never
    logged, never included in an exception, and never returned to a caller.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._connect_timeout_seconds = connect_timeout_seconds
        # `transport` is a test-only seam (BUILD_PLAN section 12 phase 4: "mock n8n
        # transport fixture for integration tests") — `httpx.MockTransport` in tests,
        # always `None` (httpx's own real network transport) in production. `verify`
        # is never exposed here or anywhere else in this class, so TLS verification
        # stays non-disableable regardless of what a caller passes (threat T-26).
        self._client = httpx.Client(base_url=self._base_url, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> N8nClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------------------
    # Internal request plumbing
    # ------------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"X-N8N-API-KEY": self._api_key}

    def _timeout(self, *, read_timeout_seconds: float) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self._connect_timeout_seconds,
            read=read_timeout_seconds,
            write=self._connect_timeout_seconds,
            pool=self._connect_timeout_seconds,
        )

    def _request(
        self,
        *,
        endpoint_template: str,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
        read_timeout_seconds: float,
    ) -> httpx.Response:
        """Every control-plane call passes through here. Raises
        :class:`~n8n_operator.errors.InstanceUnreachableError` on any transport-level
        failure (connection refused, DNS failure, TLS failure, timeout) — a
        control-plane call getting no response is not the dispatch-indeterminacy case
        ADR-005/ADR-009 govern (that is :meth:`dispatch_webhook` only); it is simply
        "the instance did not respond", reported plainly.
        """
        if endpoint_template not in ALLOWED_ENDPOINTS:
            raise ProviderError(
                _safe_message("Attempted call to an endpoint outside the allowlist.")
            )
        headers = self._headers() if authenticated else {}
        try:
            response = self._client.request(
                method,
                path,
                params=params,
                headers=headers,
                timeout=self._timeout(read_timeout_seconds=read_timeout_seconds),
            )
        except httpx.HTTPError as exc:
            raise InstanceUnreachableError() from exc
        self._check_response_size(response)
        return response

    def _check_response_size(self, response: httpx.Response) -> None:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_RESPONSE_BYTES:
                    raise ProviderError(
                        _safe_message("The n8n instance returned an oversized response.")
                    )
            except ValueError:
                pass  # a non-integer Content-Length is not this check's problem
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ProviderError(_safe_message("The n8n instance returned an oversized response."))

    def _parse_json(self, response: httpx.Response, *, not_found_message: str | None = None) -> Any:
        if response.status_code == 404 and not_found_message is not None:
            raise WorkflowMissingOnInstanceError()
        if response.is_error:
            raise ProviderError(
                _safe_message("The n8n instance returned an error response."),
                details={"status_code": response.status_code},
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(
                _safe_message("The n8n instance returned a malformed response.")
            ) from exc

    # ------------------------------------------------------------------------------
    # Public, allowlisted operations
    # ------------------------------------------------------------------------------

    def health_check(self) -> HealthStatus:
        """``GET /healthz`` — unauthenticated, used for the ``instance_reachable``
        preflight check."""
        response = self._request(
            endpoint_template="/healthz",
            method="GET",
            path="/healthz",
            authenticated=False,
            read_timeout_seconds=self._connect_timeout_seconds,
        )
        body = self._parse_json(response)
        try:
            return HealthStatus.model_validate(body)
        except Exception as exc:
            raise ProviderError(
                _safe_message("The n8n instance returned a malformed response.")
            ) from exc

    def get_api_version_info(self) -> str | None:
        """The public API's own spec version (``info.version`` in the OpenAPI
        document) — a coarse, unauthenticated proxy for instance compatibility, **not**
        the n8n release version (docs/N8N_COMPATIBILITY.md section 10: no endpoint
        returns that without a session-cookie login this client never acquires).
        Returns ``None`` if the field cannot be found, rather than raising — this is an
        advisory signal, and its absence is exactly what makes the ``compatible_version``
        preflight check ``unverifiable`` rather than ``fail``.
        """
        response = self._request(
            endpoint_template="/api/v1/openapi.yml",
            method="GET",
            path="/api/v1/openapi.yml",
            authenticated=False,
            read_timeout_seconds=self._connect_timeout_seconds,
        )
        if response.is_error:
            return None
        for line in response.text.splitlines():
            stripped = line.strip()
            if stripped.startswith("version:"):
                return stripped.removeprefix("version:").strip().strip("'\"")
        return None

    def get_workflow(self, n8n_workflow_id: str) -> dict[str, Any]:
        """``GET /api/v1/workflows/{id}`` — returns the **raw** parsed dict, after
        confirming it has at least the shape :class:`~n8n_operator.n8n.types.WorkflowDefinition`
        requires. The raw dict, not the validated model, is what
        ``n8n/canonicalization.py`` hashes (see that module's docstring for why).
        Raises :class:`~n8n_operator.errors.WorkflowMissingOnInstanceError` on a 404.
        """
        response = self._request(
            endpoint_template="/api/v1/workflows/{id}",
            method="GET",
            path=f"/api/v1/workflows/{n8n_workflow_id}",
            read_timeout_seconds=self._connect_timeout_seconds,
        )
        raw = self._parse_json(response, not_found_message="workflow not found")
        try:
            WorkflowDefinition.model_validate(raw)
        except Exception as exc:
            raise ProviderError(
                _safe_message("The n8n instance returned a malformed workflow definition.")
            ) from exc
        if not isinstance(raw, dict):
            raise ProviderError(
                _safe_message("The n8n instance returned a malformed workflow definition.")
            )
        return raw

    def list_executions(self, *, workflow_id: str, limit: int = 20) -> list[ExecutionSummary]:
        """``GET /api/v1/executions`` (paginated via ``nextCursor``), bounded by
        :data:`MAX_EXECUTION_LIST_PAGES` regardless of what the server returns."""
        results: list[ExecutionSummary] = []
        cursor: str | None = None
        for _page in range(MAX_EXECUTION_LIST_PAGES):
            params: dict[str, Any] = {"workflowId": workflow_id, "limit": limit}
            if cursor is not None:
                params["cursor"] = cursor
            response = self._request(
                endpoint_template="/api/v1/executions",
                method="GET",
                path="/api/v1/executions",
                params=params,
                read_timeout_seconds=self._connect_timeout_seconds,
            )
            body = self._parse_json(response)
            if not isinstance(body, dict):
                raise ProviderError(
                    _safe_message("The n8n instance returned a malformed response.")
                )
            for item in body.get("data", []):
                try:
                    results.append(ExecutionSummary.model_validate(item))
                except Exception as exc:
                    raise ProviderError(
                        _safe_message("The n8n instance returned a malformed execution record.")
                    ) from exc
            cursor = body.get("nextCursor")
            if cursor is None or len(results) >= limit:
                break
        return results[:limit]

    def get_execution(self, execution_id: str) -> ExecutionSummary:
        """``GET /api/v1/executions/{id}`` — **never** with ``includeData=true``. The
        full per-node ``runData`` tree (including a webhook trigger's raw inbound
        request) is never fetched *by this method* — see ``n8n/types.py``'s
        ``ExecutionSummary`` docstring. There is nothing this method could
        accidentally leak that it never read in the first place.

        :meth:`get_execution_node_trace` is the one narrow, deliberate exception to
        "never ``includeData=true``" this client makes, for ``get_execution_log``
        specifically — see that method's own docstring for how it stays safe anyway.
        """
        response = self._request(
            endpoint_template="/api/v1/executions/{id}",
            method="GET",
            path=f"/api/v1/executions/{execution_id}",
            read_timeout_seconds=self._connect_timeout_seconds,
        )
        body = self._parse_json(response, not_found_message="execution not found")
        try:
            return ExecutionSummary.model_validate(body)
        except Exception as exc:
            raise ProviderError(
                _safe_message("The n8n instance returned a malformed execution record.")
            ) from exc

    def get_execution_node_trace(self, execution_id: str) -> dict[str, Any] | None:
        """``GET /api/v1/executions/{id}?includeData=true`` — the *one* place this
        client ever requests full run data, scoped as narrowly as physically possible:
        this method reads exactly five scalar fields per node run (name, type,
        execution status, duration, and — for the first failed node only — a
        best-effort error message) and discards everything else immediately.

        **What is never read into a Python value, let alone returned:** each node
        run's own ``data`` field — its actual input/output — which can carry a webhook
        trigger's raw inbound request verbatim (headers, query, body —
        docs/N8N_COMPATIBILITY.md section 8, the empirical basis for this method's
        shape). Every value below comes from ``dict.get`` on a *named* key, built into
        a *new* dict of primitives; there is no code path through which a nested
        object from the response is forwarded wholesale, so a surprise in n8n's exact
        schema can make a field come back missing, never leak something unintended.

        Node order comes from n8n's own ``executionIndex`` (confirmed present on every
        run in the empirical fixture this shape is based on), not dict insertion order
        — ``runData`` is keyed by node name, unordered.

        Returns ``None`` (never a partial guess) if the response cannot be confidently
        read as this shape at all: an unreachable instance, a 404, or a response body
        that isn't the expected structure.
        """
        try:
            response = self._request(
                endpoint_template="/api/v1/executions/{id}",
                method="GET",
                path=f"/api/v1/executions/{execution_id}",
                params={"includeData": "true"},
                read_timeout_seconds=self._connect_timeout_seconds,
            )
            body = self._parse_json(response, not_found_message="execution not found")
        except ProviderError:
            return None
        if not isinstance(body, dict):
            return None

        data = body.get("data")
        result_data = data.get("resultData") if isinstance(data, dict) else None
        run_data = result_data.get("runData") if isinstance(result_data, dict) else None
        if not isinstance(run_data, dict):
            return None

        node_types: dict[str, str] = {}
        workflow_data = body.get("workflowData")
        if isinstance(workflow_data, dict):
            for node in workflow_data.get("nodes") or []:
                if isinstance(node, dict) and isinstance(node.get("name"), str):
                    node_types[node["name"]] = str(node.get("type", "unknown"))

        ordered: list[tuple[int, dict[str, Any]]] = []
        failed_node: str | None = None
        failed_node_error: str | None = None
        for name, attempts in run_data.items():
            if not isinstance(attempts, list) or not attempts or not isinstance(name, str):
                continue
            last = attempts[-1]
            if not isinstance(last, dict):
                continue
            status = last.get("executionStatus")
            status_str = status if isinstance(status, str) else "unknown"
            duration = last.get("executionTime")
            duration_ms = duration if isinstance(duration, int) else None
            index = last.get("executionIndex")
            order = index if isinstance(index, int) else len(ordered)
            ordered.append(
                (
                    order,
                    {
                        "name": name,
                        "type": node_types.get(name, "unknown"),
                        "status": status_str,
                        "duration_ms": duration_ms,
                    },
                )
            )
            if status_str != "success" and failed_node is None:
                failed_node = name
                error = last.get("error")
                message = error.get("message") if isinstance(error, dict) else None
                failed_node_error = message if isinstance(message, str) else "An error occurred."

        ordered.sort(key=lambda pair: pair[0])
        return {
            "nodes": [entry for _order, entry in ordered],
            "failed_node": failed_node,
            "failed_node_error": failed_node_error,
        }

    def known_secrets(self) -> tuple[str, ...]:
        """The credential values this client holds (BUILD_PLAN section 8.1/ADR-006):
        currently just the API key. Exposed only so the composition root can pass them
        to ``core.redaction.scrub_secrets`` as defense in depth against a secret
        appearing verbatim in an execution result — not a new exposure, since only
        code that already constructed this client (and so already has the key) can
        call it."""
        return (self._api_key,)

    def dispatch_webhook(
        self,
        *,
        path: str,
        method: str,
        json_body: dict[str, Any],
        timeout_seconds: float,
    ) -> DispatchOutcome:
        """The one call in this codebase with an external side effect. ``path`` is a
        registry ``trigger.path`` value verbatim — BUILD_PLAN section 6.3: "Path
        component only, e.g. ``/webhook/abc123``" — the **full** path including n8n's
        own ``/webhook/`` prefix, not a bare suffix. Rule R8 already forbids it being an
        absolute URL at registry-load time; this method resolves it against this
        instance's own base URL and nothing else, so it is never a caller-supplied full
        URL either.

        **No retry, ever.** A transport-level failure of any kind — timeout, connection
        refused, connection reset, TLS failure — becomes
        ``DispatchOutcome(kind="indeterminate", ...)``, uniformly, with no attempt to
        distinguish "definitely did not happen" from "response lost" (ADR-005's
        alternatives-considered section explains why that distinction is not safe to
        build). A non-2xx HTTP response that *did* arrive is ``kind="error"`` — n8n
        responded, so nothing here is ambiguous about whether the request was received.
        """
        url = f"{self._base_url}{path}"
        try:
            response = self._client.request(
                method,
                url,
                json=json_body,
                timeout=self._timeout(read_timeout_seconds=timeout_seconds),
            )
        except httpx.HTTPError:
            return DispatchOutcome(
                kind="indeterminate",
                http_status=None,
                result=None,
                execution_id=None,
                correlation_available=False,
            )

        try:
            self._check_response_size(response)
        except ProviderError:
            return DispatchOutcome(
                kind="indeterminate",
                http_status=response.status_code,
                result=None,
                execution_id=None,
                correlation_available=False,
            )

        try:
            body = response.json()
        except ValueError:
            # The response body itself could not be parsed as JSON at all — ADR-009
            # section 1.1 treats an unparseable response the same as a lost one, not
            # as a confirmed outcome. This is distinct from a *malformed envelope*
            # below, where the body parses fine but doesn't carry a usable
            # `n8n_operator` block: that case still confirms success/error from the
            # HTTP status, only correlation is unavailable.
            return DispatchOutcome(
                kind="indeterminate",
                http_status=response.status_code,
                result=None,
                execution_id=None,
                correlation_available=False,
            )

        result: Any = body
        execution_id: str | None = None
        if isinstance(body, dict) and "n8n_operator" in body:
            try:
                envelope = ResponseEnvelope.model_validate(body)
            except Exception:
                envelope = None
            if envelope is not None and envelope.execution_id is not None:
                execution_id = envelope.execution_id
                result = envelope.data

        kind: Literal["success", "error"] = "success" if response.is_success else "error"
        return DispatchOutcome(
            kind=kind,
            http_status=response.status_code,
            result=result,
            execution_id=execution_id,
            correlation_available=execution_id is not None,
        )
