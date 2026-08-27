"""The loopback FastAPI approval app end to end (BUILD_PLAN section 12, phase 6;
ADR-010) — GET rendering, CSRF, Origin/Host validation, reused/expired/invalid tokens,
concurrent approve/reject, safe headers, and token hygiene in logs and storage.

Uses ``fastapi.testclient.TestClient`` with ``base_url`` set to the configured loopback
bind, so ``Host`` matches what the app expects without every test setting it by hand;
``Origin`` is never sent automatically by the client (matching a real browser only on
same-origin GET navigation, not on POST) and is set explicitly per test.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.approval.app import build_app
from n8n_operator.core import service
from n8n_operator.core.models import PreflightResult
from n8n_operator.storage.repository import ApprovalRepository, PrincipalRepository
from n8n_operator.storage.session import session_scope

APPROVAL_BIND = "127.0.0.1:8765"
EXPECTED_ORIGIN = f"http://{APPROVAL_BIND}"

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: phase6-app-test
workflows:
  - id: wf.approval
    n8n_workflow_id: n8n-1
    title: Needs approval
    description: Writes to an external system.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_a}
    risk: medium
    side_effects: external_write
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/a
      auth: none
    input_schema:
      type: object
      properties:
        email: {{type: string}}
      required: [email]
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
""".format(hash_a="a" * 64)


class FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(REGISTRY_YAML)
    return path


@pytest.fixture
def env(session_factory: sessionmaker[Session], registry_path: Path) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="local", kind="local", display_name="local")
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
    return {"principal_id": "local", "environment": "default"}


def _prepare(session_factory: sessionmaker[Session], env: dict[str, Any]) -> tuple[str, str]:
    """Prepare ``wf.approval`` and return ``(operation_id, approval_token)``."""
    with session_scope(session_factory) as session:
        operation, _replay, token = service.prepare_operation(
            session,
            principal_id=env["principal_id"],
            environment=env["environment"],
            workflow_id="wf.approval",
            arguments={"email": "a@b.com"},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
        )
        assert token is not None
        return operation.id, token


@pytest.fixture
def client(session_factory: sessionmaker[Session], env: dict[str, Any]) -> Iterator[TestClient]:
    app = build_app(APPROVAL_BIND, session_factory)
    with TestClient(app, base_url=EXPECTED_ORIGIN) as test_client:
        yield test_client


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None, html
    return match.group(1)


# --------------------------------------------------------------------------------------
# GET rendering.
# --------------------------------------------------------------------------------------


def test_get_pending_approval_renders_the_decision_page(
    client: TestClient, session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    _op_id, token = _prepare(session_factory, env)
    response = client.get(f"/approve/{token}")
    assert response.status_code == 200
    assert "Needs approval" in response.text
    assert "medium" in response.text
    assert "external_write" in response.text
    assert "a@b.com" in response.text
    assert f'action="/approve/{token}"' in response.text
    assert f'action="/reject/{token}"' in response.text


def test_get_invalid_token_returns_404_with_error_banner(client: TestClient) -> None:
    response = client.get("/approve/not-a-real-token")
    assert response.status_code == 404
    assert "not valid" in response.text


def test_get_wrong_host_header_is_rejected(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    app = build_app(APPROVAL_BIND, session_factory)
    _op_id, token = _prepare(session_factory, env)
    with TestClient(app, base_url="http://evil.example") as evil_client:
        response = evil_client.get(f"/approve/{token}")
    assert response.status_code == 400


def test_get_response_carries_no_store_and_no_framing_headers(
    client: TestClient, session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    _op_id, token = _prepare(session_factory, env)
    response = client.get(f"/approve/{token}")
    assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


# --------------------------------------------------------------------------------------
# Approve / reject happy paths.
# --------------------------------------------------------------------------------------


def test_post_approve_with_valid_csrf_succeeds(
    client: TestClient, session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, token = _prepare(session_factory, env)
    get_response = client.get(f"/approve/{token}")
    csrf = _extract_csrf(get_response.text)

    post_response = client.post(
        f"/approve/{token}",
        data={"csrf_token": csrf},
        headers={"origin": EXPECTED_ORIGIN},
    )
    assert post_response.status_code == 200
    assert "Decision recorded: approved" in post_response.text

    with session_scope(session_factory) as session:
        operation = service.get_operation(session, operation_id=op_id, principal_id="local")
    assert operation.state == "APPROVED"


def test_post_reject_with_valid_csrf_succeeds(
    client: TestClient, session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, token = _prepare(session_factory, env)
    get_response = client.get(f"/approve/{token}")
    csrf = _extract_csrf(get_response.text)

    post_response = client.post(
        f"/reject/{token}",
        data={"csrf_token": csrf},
        headers={"origin": EXPECTED_ORIGIN},
    )
    assert post_response.status_code == 200
    assert "Decision recorded: rejected" in post_response.text

    with session_scope(session_factory) as session:
        operation = service.get_operation(session, operation_id=op_id, principal_id="local")
    assert operation.state == "REJECTED"


# --------------------------------------------------------------------------------------
# CSRF.
# --------------------------------------------------------------------------------------


def test_post_without_ever_getting_first_has_no_csrf_cookie_and_is_rejected(
    client: TestClient, session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    _op_id, token = _prepare(session_factory, env)
    response = client.post(
        f"/approve/{token}",
        data={"csrf_token": "guessed-value"},
        headers={"origin": EXPECTED_ORIGIN},
    )
    assert response.status_code == 403
    assert "expired" in response.text or "not submitted" in response.text


def test_post_with_wrong_csrf_form_value_is_rejected(
    client: TestClient, session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, token = _prepare(session_factory, env)
    get_response = client.get(f"/approve/{token}")
    _real_csrf = _extract_csrf(get_response.text)

    response = client.post(
        f"/approve/{token}",
        data={"csrf_token": "a-different-value-entirely"},
        headers={"origin": EXPECTED_ORIGIN},
    )
    assert response.status_code == 403

    with session_scope(session_factory) as session:
        operation = service.get_operation(session, operation_id=op_id, principal_id="local")
    assert operation.state == "PENDING_APPROVAL"


def test_post_missing_origin_header_is_rejected(
    client: TestClient, session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    _op_id, token = _prepare(session_factory, env)
    get_response = client.get(f"/approve/{token}")
    csrf = _extract_csrf(get_response.text)

    response = client.post(f"/approve/{token}", data={"csrf_token": csrf})
    assert response.status_code == 403


def test_post_wrong_origin_header_is_rejected(
    client: TestClient, session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    _op_id, token = _prepare(session_factory, env)
    get_response = client.get(f"/approve/{token}")
    csrf = _extract_csrf(get_response.text)

    response = client.post(
        f"/approve/{token}",
        data={"csrf_token": csrf},
        headers={"origin": "http://evil.example"},
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------------------
# Reused / expired / not-pending tokens, and cross-operation isolation.
# --------------------------------------------------------------------------------------


def test_reused_token_is_rejected_at_the_http_layer(
    client: TestClient, session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    _op_id, token = _prepare(session_factory, env)
    get_response = client.get(f"/approve/{token}")
    csrf = _extract_csrf(get_response.text)
    first = client.post(
        f"/approve/{token}", data={"csrf_token": csrf}, headers={"origin": EXPECTED_ORIGIN}
    )
    assert first.status_code == 200

    second_get = client.get(f"/approve/{token}")
    assert second_get.status_code == 409
    assert "already been used" in second_get.text


def test_expired_operation_token_is_rejected_at_the_http_layer(
    client: TestClient, session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    from datetime import timedelta

    from sqlalchemy import update

    from n8n_operator.storage.models import Operation as OperationRow

    op_id, token = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        session.execute(
            update(OperationRow)
            .where(OperationRow.id == op_id)
            .values(approval_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    response = client.get(f"/approve/{token}")
    assert response.status_code == 409
    assert "no longer awaiting approval" in response.text


def test_deciding_one_operation_never_affects_another(
    client: TestClient, session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    """'Wrong operation' isolation: the web channel has no field for a client-supplied
    operation ID at all — the URL's token is the only thing that ever selects which
    operation a decision applies to."""
    op_a, token_a = _prepare(session_factory, env)
    op_b, token_b = _prepare(session_factory, env)

    get_a = client.get(f"/approve/{token_a}")
    csrf_a = _extract_csrf(get_a.text)
    response = client.post(
        f"/approve/{token_a}", data={"csrf_token": csrf_a}, headers={"origin": EXPECTED_ORIGIN}
    )
    assert response.status_code == 200

    with session_scope(session_factory) as session:
        operation_a = service.get_operation(session, operation_id=op_a, principal_id="local")
        operation_b = service.get_operation(session, operation_id=op_b, principal_id="local")
    assert operation_a.state == "APPROVED"
    assert operation_b.state == "PENDING_APPROVAL"

    # token_b is still fully usable — op_a's decision touched nothing about it.
    get_b = client.get(f"/approve/{token_b}")
    assert get_b.status_code == 200


# --------------------------------------------------------------------------------------
# Concurrent approve/reject on the same operation.
# --------------------------------------------------------------------------------------


def test_concurrent_approve_and_reject_only_one_wins(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    app = build_app(APPROVAL_BIND, session_factory)
    op_id, token = _prepare(session_factory, env)

    with TestClient(app, base_url=EXPECTED_ORIGIN) as setup_client:
        get_response = setup_client.get(f"/approve/{token}")
        csrf = _extract_csrf(get_response.text)
        cookie = setup_client.cookies.get("n8n_operator_csrf")
    assert cookie is not None

    results: list[int] = []
    lock = threading.Lock()

    def _decide(path: str) -> None:
        with TestClient(app, base_url=EXPECTED_ORIGIN) as thread_client:
            thread_client.cookies.set("n8n_operator_csrf", cookie)
            response = thread_client.post(
                f"/{path}/{token}",
                data={"csrf_token": csrf},
                headers={"origin": EXPECTED_ORIGIN},
            )
        with lock:
            results.append(response.status_code)

    threads = [
        threading.Thread(target=_decide, args=("approve",)),
        threading.Thread(target=_decide, args=("reject",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 409]  # exactly one decision landed

    with session_scope(session_factory) as session:
        operation = service.get_operation(session, operation_id=op_id, principal_id="local")
    assert operation.state in ("APPROVED", "REJECTED")


# --------------------------------------------------------------------------------------
# Token hygiene: never in logs, never at rest.
# --------------------------------------------------------------------------------------


def test_token_never_appears_in_logs(
    client: TestClient,
    session_factory: sessionmaker[Session],
    env: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scoped to the ``n8n_operator`` logger namespace, not the root logger: the test
    HTTP client's own transport (``httpx2``) logs the full request line — including
    the token, which lives in the URL path by design, exactly as documented ("POST
    /approve/{token}") — at INFO level as part of *its* debugging output. That is
    client-side test-harness logging, not this application writing the token anywhere;
    what boundary B10/AC-21's "no token in logs" actually constrains is our own code.
    """
    op_id, token = _prepare(session_factory, env)
    with caplog.at_level(logging.DEBUG, logger="n8n_operator"):
        get_response = client.get(f"/approve/{token}")
        csrf = _extract_csrf(get_response.text)
        client.post(
            f"/approve/{token}", data={"csrf_token": csrf}, headers={"origin": EXPECTED_ORIGIN}
        )

    for record in caplog.records:
        assert record.name.startswith("n8n_operator")
        assert token not in record.getMessage()
    assert op_id  # the operation ID itself is fine to appear; only the raw token is not


def test_token_never_stored_only_its_hash(
    client: TestClient, session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    op_id, token = _prepare(session_factory, env)
    with session_scope(session_factory) as session:
        approval = ApprovalRepository(session).get_by_operation_id(op_id)
        assert approval is not None
        assert approval.token_hash != token
        assert token not in approval.token_hash
