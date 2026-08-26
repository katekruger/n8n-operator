"""Contract: no automatic retry anywhere in the n8n client (ADR-005, AC-17).

Static source inspection, not a runtime behavior test — a grep-based check is exactly
what AC-17 asks for, and it catches retry machinery whether or not any current test
happens to exercise the code path that would use it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
N8N_CLIENT_SOURCE = REPO_ROOT / "src" / "n8n_operator" / "n8n" / "client.py"

FORBIDDEN_SUBSTRINGS = (
    "retries=",
    "Retry(",
    "retry_strategy",
    "backoff",
    "tenacity",
    "urllib3.util.retry",
    "HTTPAdapter",
    "max_retries",
)


@pytest.mark.contract
def test_n8n_client_source_exists() -> None:
    """A forward-looking regression guard: the tests below are only meaningful once
    this file exists at all."""
    assert N8N_CLIENT_SOURCE.is_file()


@pytest.mark.contract
@pytest.mark.parametrize("forbidden", FORBIDDEN_SUBSTRINGS)
def test_no_retry_machinery_appears_in_the_n8n_client(forbidden: str) -> None:
    text = N8N_CLIENT_SOURCE.read_text(encoding="utf-8")
    assert forbidden not in text, (
        f"{forbidden!r} found in n8n/client.py — no retry logic is permitted (ADR-005)"
    )


@pytest.mark.contract
def test_no_retry_machinery_anywhere_under_the_n8n_package() -> None:
    n8n_package = REPO_ROOT / "src" / "n8n_operator" / "n8n"
    for path in sorted(n8n_package.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_SUBSTRINGS:
            assert forbidden not in text, f"{forbidden!r} found in {path.relative_to(REPO_ROOT)}"


@pytest.mark.contract
def test_httpx_client_is_constructed_without_a_retries_transport() -> None:
    """A narrower, more targeted check than the substring grep above: confirms
    ``httpx.Client(...)`` itself is never constructed with a ``transport=`` argument
    that could smuggle in retry behavior via ``httpx.HTTPTransport(retries=N)`` — the
    only ``transport=`` usage in this module is the test-injection seam, which always
    passes ``None`` in production."""
    text = N8N_CLIENT_SOURCE.read_text(encoding="utf-8")
    assert "HTTPTransport(" not in text
    assert "AsyncHTTPTransport(" not in text
