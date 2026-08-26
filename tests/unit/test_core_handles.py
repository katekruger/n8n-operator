"""``core/handles.py``: minting operation handles and approval tokens (BUILD_PLAN
section 12, phase 3)."""

from __future__ import annotations

import hashlib

import pytest

from n8n_operator.core.handles import mint_approval_token, mint_operation_handle


@pytest.mark.unit
def test_mint_operation_handle_has_the_op_prefix() -> None:
    assert mint_operation_handle().startswith("op_")


@pytest.mark.unit
def test_mint_operation_handle_is_unique_across_many_calls() -> None:
    handles = {mint_operation_handle() for _ in range(1000)}
    assert len(handles) == 1000


@pytest.mark.unit
def test_mint_operation_handle_is_a_valid_ulid_after_the_prefix() -> None:
    from ulid import ULID

    handle = mint_operation_handle()
    ULID.from_str(handle.removeprefix("op_"))  # raises if malformed


@pytest.mark.unit
def test_mint_approval_token_hash_matches_a_plain_sha256_of_the_token() -> None:
    minted = mint_approval_token()
    expected = hashlib.sha256(minted.token.encode("utf-8")).hexdigest()
    assert minted.token_hash == expected


@pytest.mark.unit
def test_mint_approval_token_is_unique_across_many_calls() -> None:
    tokens = {mint_approval_token().token for _ in range(1000)}
    hashes = {mint_approval_token().token_hash for _ in range(1000)}
    assert len(tokens) == 1000
    assert len(hashes) == 1000


@pytest.mark.unit
def test_mint_approval_token_is_reasonably_long_and_url_safe() -> None:
    minted = mint_approval_token()
    assert len(minted.token) >= 32
    # url-safe base64 alphabet only
    assert all(c.isalnum() or c in "-_" for c in minted.token)
