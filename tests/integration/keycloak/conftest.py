"""Fixtures for the pinned, loopback-only Keycloak integration harness.

Every test module under this package is marked ``keycloak`` and skips unless
``N8N_OPERATOR_TEST_KEYCLOAK_URL`` is set — mirrors
``tests/integration/postgres/conftest.py``'s ``N8N_OPERATOR_TEST_POSTGRES_URL`` pattern
exactly: a base URL for a running Keycloak instance with the realm in
``docker/keycloak-test/realm-export.json`` already imported. CI provides this via a
service container plus a realm-import step
(``.github/workflows/ci.yml`` ``keycloak`` job); a local run provides it via::

    docker compose -f docker/keycloak-test/docker-compose.yml up -d
    export N8N_OPERATOR_TEST_KEYCLOAK_URL=http://127.0.0.1:8081

— opt-in, not a default part of the suite. Local compose imports the realm itself via
``--import-realm``; the CI job POSTs the same ``realm-export.json`` to Keycloak's admin
REST API instead, since a bare GitHub Actions service container has no bind-mount.

The realm/client/user identifiers and secret below are exactly the fixture values
declared in ``docker/keycloak-test/realm-export.json`` — fixed, disposable, dev-and-CI
-only values, never read from a secret store, never valid outside this harness.
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.keycloak

REALM_NAME = "n8n-operator-test"
CLIENT_ID = "n8n-operator"
# Throwaway dev/CI-only secret, hardcoded (and clearly commented as such) in
# docker/keycloak-test/realm-export.json. Never a production credential.
CLIENT_SECRET = "n8n-operator-test-harness-secret-do-not-use-in-prod"
TEST_USERNAME = "alice"
TEST_PASSWORD = "alice-test-password"


def _base_url() -> str:
    url = os.environ.get("N8N_OPERATOR_TEST_KEYCLOAK_URL")
    if not url:
        pytest.skip("N8N_OPERATOR_TEST_KEYCLOAK_URL is required for keycloak tests")
    return url.rstrip("/")


def password_grant(token_endpoint: str, *, username: str, password: str) -> httpx.Response:
    """POST a real Resource Owner Password Credentials grant to Keycloak's real token
    endpoint. Returns the raw response so callers can assert on both success (a real
    signed token) and failure (e.g. a wrong password, rejected by Keycloak itself,
    never reaching this codebase's OidcVerifier at all).
    """
    return httpx.post(
        token_endpoint,
        data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "username": username,
            "password": password,
            "scope": "openid",
        },
        timeout=10.0,
    )


@pytest.fixture
def keycloak_base_url() -> str:
    return _base_url()


@pytest.fixture
def keycloak_issuer_url(keycloak_base_url: str) -> str:
    """The real issuer URL Keycloak puts in every token's `iss` claim and serves its
    discovery document from, at ``<issuer>/.well-known/openid-configuration``."""
    return f"{keycloak_base_url}/realms/{REALM_NAME}"


@pytest.fixture
def keycloak_token_endpoint(keycloak_base_url: str) -> str:
    return f"{keycloak_base_url}/realms/{REALM_NAME}/protocol/openid-connect/token"


@pytest.fixture
def real_access_token(keycloak_token_endpoint: str) -> str:
    """A real, signed access token for `alice`, obtained from the real Keycloak token
    endpoint via the Resource Owner Password Credentials grant."""
    response = password_grant(
        keycloak_token_endpoint, username=TEST_USERNAME, password=TEST_PASSWORD
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    assert isinstance(token, str)
    return token
