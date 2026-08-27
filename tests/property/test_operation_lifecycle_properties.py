"""Hypothesis properties over the full ``prepare_operation`` use case, against a real
database (BUILD_PLAN section 10.2, phase 3):

- Fingerprint stability: canonicalization is idempotent, and key reordering or
  insignificant whitespace does not change the fingerprint (I5).
- Fingerprint sensitivity: for any two structurally different JSON values, the
  fingerprints differ.
- Idempotency: for any pair of ``prepare_operation`` calls sharing a namespace
  ``(principal, environment, workflow_id, key)``, one operation exists afterwards;
  differing in any namespace component yields two (I8).
- Argument limits: for any payload whose canonical serialization exceeds the effective
  limit, no operation row is written and the error is ``ARGUMENTS_TOO_LARGE`` (I10).
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from n8n_operator.core import service
from n8n_operator.core.idempotency import canonicalize_arguments, fingerprint_arguments
from n8n_operator.core.models import Operation, PreflightResult
from n8n_operator.errors import ArgumentsTooLargeError, IdempotencyConflictError
from n8n_operator.storage.models import Base
from n8n_operator.storage.repository import OperationRepository, PrincipalRepository
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

SAFE_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: prop-test
workflows:
  - id: wf.a
    n8n_workflow_id: n8n-1
    title: A
    description: B
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_a}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/a
      auth: none
    input_schema:
      type: object
      additionalProperties: false
  - id: wf.b
    n8n_workflow_id: n8n-2
    title: B
    description: C
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_b}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/b
      auth: none
    input_schema:
      type: object
      additionalProperties: false
""".format(hash_a="a" * 64, hash_b="b" * 64)


class _FakePreflight:
    def check(self, workflow: object) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


class _Env:
    """A fresh, isolated SQLite database with the schema and a loaded registry."""

    def __init__(self) -> None:
        fd, path_str = tempfile.mkstemp(suffix=".db")
        import os

        os.close(fd)
        self.db_path = Path(path_str)
        self.db_path.unlink()  # sqlite creates its own file; only the name is needed
        self.engine = create_engine_for_url(f"sqlite+pysqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.session_factory = create_session_factory(self.engine)

        registry_fd, registry_path_str = tempfile.mkstemp(suffix=".yaml")
        os.close(registry_fd)
        self.registry_path = Path(registry_path_str)
        self.registry_path.write_text(REGISTRY_YAML)

        with session_scope(self.session_factory) as session:
            PrincipalRepository(session).create(id="local", kind="local", display_name="local")
            service.reload_registry(session, self.registry_path, server_max_argument_bytes=262_144)

    def prepare(
        self,
        *,
        workflow_id: str = "wf.a",
        arguments: dict[str, object] | None = None,
        idempotency_key: str | None = None,
        environment: str = "default",
        principal_id: str = "local",
        server_max_argument_bytes: int = 262_144,
    ) -> tuple[Operation, bool]:
        with session_scope(self.session_factory) as session:
            operation, replay, _token = service.prepare_operation(
                session,
                principal_id=principal_id,
                environment=environment,
                workflow_id=workflow_id,
                arguments=arguments or {},
                preflight=_FakePreflight(),
                server_max_argument_bytes=server_max_argument_bytes,
                idempotency_key=idempotency_key,
            )
            return operation, replay

    def operation_count(self) -> int:
        """Counts every operation for the ``local`` principal across every
        environment — deliberately not filtered to ``default``, since a namespace test
        may prepare one under a different environment on purpose."""
        with session_scope(self.session_factory) as session:
            return len(OperationRepository(session).list(principal_id="local", limit=1000))

    def close(self) -> None:
        self.engine.dispose()
        self.db_path.unlink(missing_ok=True)
        self.registry_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------------------
# Fingerprint stability and sensitivity (I5) — pure, no DB needed.
# --------------------------------------------------------------------------------------

_json_leaf = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-1000, max_value=1000)
    | st.text(alphabet=SAFE_ALPHABET, max_size=10)
)
_json_object = st.dictionaries(
    st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=8),
    _json_leaf | st.lists(_json_leaf, max_size=3),
    max_size=5,
)


@given(value=_json_object)
@settings(max_examples=50)
def test_fingerprint_is_stable_under_canonicalization_idempotence(value: dict[str, object]) -> None:
    once = fingerprint_arguments(canonicalize_arguments(value))
    roundtripped = json.loads(canonicalize_arguments(value).decode())
    twice = fingerprint_arguments(canonicalize_arguments(roundtripped))
    assert once == twice


@given(value=_json_object)
@settings(max_examples=50)
def test_fingerprint_is_stable_under_key_reordering(value: dict[str, object]) -> None:
    reordered = dict(reversed(list(value.items())))
    assert fingerprint_arguments(canonicalize_arguments(value)) == fingerprint_arguments(
        canonicalize_arguments(reordered)
    )


@given(a=_json_object, b=_json_object)
@settings(max_examples=50)
def test_structurally_different_values_have_different_fingerprints(
    a: dict[str, object], b: dict[str, object]
) -> None:
    if a == b:
        return
    fp_a = fingerprint_arguments(canonicalize_arguments(a))
    fp_b = fingerprint_arguments(canonicalize_arguments(b))
    assert fp_a != fp_b


# --------------------------------------------------------------------------------------
# Idempotency (I8) — DB-backed.
# --------------------------------------------------------------------------------------


@given(
    key=st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=10),
    email=st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=10),
)
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_same_namespace_and_key_same_fingerprint_yields_one_operation(key: str, email: str) -> None:
    env = _Env()
    try:
        op1, replay1 = env.prepare(arguments={"email": email}, idempotency_key=key)
        op2, replay2 = env.prepare(arguments={"email": email}, idempotency_key=key)
        assert op1.id == op2.id
        assert replay1 is False
        assert replay2 is True
        assert env.operation_count() == 1
    finally:
        env.close()


@given(key=st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=10))
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_same_namespace_and_key_different_fingerprint_conflicts(key: str) -> None:
    env = _Env()
    try:
        env.prepare(arguments={"email": "a"}, idempotency_key=key)
        with pytest.raises(IdempotencyConflictError):
            env.prepare(arguments={"email": "different"}, idempotency_key=key)
        assert env.operation_count() == 1
    finally:
        env.close()


@given(
    key=st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=10),
    differ_by=st.sampled_from(["workflow_id", "environment"]),
)
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_differing_in_any_namespace_component_yields_two_operations(
    key: str, differ_by: str
) -> None:
    env = _Env()
    try:
        op1, _ = env.prepare(workflow_id="wf.a", arguments={}, idempotency_key=key)
        if differ_by == "workflow_id":
            op2, _ = env.prepare(workflow_id="wf.b", arguments={}, idempotency_key=key)
        else:
            op2, _ = env.prepare(
                workflow_id="wf.a", arguments={}, idempotency_key=key, environment="staging"
            )
        assert op1.id != op2.id
        assert env.operation_count() == 2
    finally:
        env.close()


# --------------------------------------------------------------------------------------
# Argument limits (I10) — DB-backed.
# --------------------------------------------------------------------------------------


@given(
    limit=st.integers(min_value=50, max_value=500), overage=st.integers(min_value=1, max_value=500)
)
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_arguments_exceeding_the_effective_limit_write_no_operation_row(
    limit: int, overage: int
) -> None:
    env = _Env()
    try:
        payload_size = limit + overage
        before = env.operation_count()
        with pytest.raises(ArgumentsTooLargeError):
            env.prepare(arguments={"junk": "x" * payload_size}, server_max_argument_bytes=limit)
        after = env.operation_count()
        assert after == before
    finally:
        env.close()


@given(limit=st.integers(min_value=200, max_value=1000))
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_arguments_within_the_effective_limit_are_accepted(limit: int) -> None:
    env = _Env()
    try:
        # A tiny payload, always well under any limit in this range.
        operation, _replay = env.prepare(arguments={"a": "x"}, server_max_argument_bytes=limit)
        assert operation.state in ("APPROVED", "PENDING_APPROVAL", "INVALID", "BLOCKED")
    finally:
        env.close()
