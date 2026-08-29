"""integrations/agent_audit.py: optional, and must never affect n8n-operator's
own behavior whether or not agent_audit_record is installed.

The 'no package installed' tests below run unconditionally -- that is the
default state of this repository's own dependency set, and is exactly the
state these tests exist to protect. The mapping-correctness tests are
skipped unless the optional 'agent-audit' dependency group is installed
(`uv sync --group agent-audit`), since they need the real package.
"""

from __future__ import annotations

import importlib

import pytest

from n8n_operator.integrations import agent_audit


class TestNeverRaisesRegardlessOfInstallState:
    """These assertions hold whether or not agent_audit_record is
    installed -- that's the whole point of this integration being optional.
    """

    def test_emit_proposed_does_not_raise(self) -> None:
        agent_audit.emit_proposed(
            operation_id="op_test", principal_id="p1", environment="default", workflow_id="wf1"
        )

    def test_emit_transition_does_not_raise_for_every_transition(self) -> None:
        for transition_id in [
            "T01",
            "T02",
            "T03",
            "T04",
            "T05",
            "T06",
            "T07",
            "T08",
            "T09",
            "T10",
            "T11",
            "T12",
            "T13",
            "T14",
            "T15",
            "not-a-real-transition",
        ]:
            agent_audit.emit_transition(transition_id, operation_id="op_test", actor="a1")

    def test_emit_prepare_denied_does_not_raise(self) -> None:
        agent_audit.emit_prepare_denied(
            workflow_id="wf1", principal_id="p1", environment="default", reason="denied"
        )


@pytest.mark.skipif(
    importlib.util.find_spec("agent_audit_record") is None,
    reason="requires the optional 'agent-audit' dependency group (uv sync --group agent-audit)",
)
class TestMappingAgainstTheRealEmitter:
    """Runs only when agent_audit_record is actually installed -- verifies the
    transition mapping is correct, not just that it doesn't crash.
    """

    def _capture(self):
        from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor

        class Capture(LogRecordProcessor):
            def __init__(self) -> None:
                self.records: list[dict[str, object]] = []
                self.event_names: list[str | None] = []

            def on_emit(self, log_record) -> None:  # type: ignore[no-untyped-def]
                self.records.append(dict(log_record.log_record.attributes or {}))
                self.event_names.append(log_record.log_record.event_name)

            def force_flush(self, timeout_millis: int = 30_000) -> bool:
                return True

            def shutdown(self) -> None:
                pass

        cap = Capture()
        provider = LoggerProvider()
        provider.add_log_record_processor(cap)
        return cap, provider

    def test_full_lifecycle_emits_the_expected_decisions_and_outcomes(self) -> None:
        # Deliberately does not fetch spec/schema/v1/agent-audit.schema.json over the
        # network: agent-audit has not published a stable tagged URL yet (verified
        # separately, by hand, against the actual schema file in that repo -- see the
        # agent-audit PR this integration accompanies). Asserting the mapping's shape
        # directly here keeps this test hermetic and avoids depending on agent-audit's
        # unreleased main branch or network access in CI.
        from agent_audit_record import Emitter

        cap, provider = self._capture()
        agent_audit._EMITTER = Emitter(logger_provider=provider)

        for transition_id in ["T02", "T03", "T05", "T06", "T07", "T08", "T09"]:
            op_id = f"op-{transition_id}"
            agent_audit.emit_proposed(
                operation_id=op_id, principal_id="p1", environment="default", workflow_id="wf1"
            )
            agent_audit.emit_transition(transition_id, operation_id=op_id, actor="actor1")

        for transition_id in ["T11", "T12", "T13", "T14", "T15"]:
            op_id = f"op-{transition_id}"
            agent_audit.emit_proposed(
                operation_id=op_id, principal_id="p1", environment="default", workflow_id="wf1"
            )
            agent_audit.emit_transition("T06", operation_id=op_id, actor="approver")
            agent_audit.emit_transition(transition_id, operation_id=op_id, actor="actor1")

        agent_audit.emit_prepare_denied(
            workflow_id="wf1", principal_id="p1", environment="default", reason="denied"
        )

        assert len(cap.records) == 31
        by_action_id: dict[str, list[dict[str, object]]] = {}
        for record in cap.records:
            by_action_id.setdefault(str(record["agent_audit.action.id"]), []).append(record)

        expected_decisions = {
            "op-T02": "auto_deny",
            "op-T03": "deny",
            "op-T05": "auto_allow",
            "op-T06": "allow",
            "op-T07": "deny",
            "op-T08": "timeout",
            "op-T09": "cancel",
        }
        for op_id, decision in expected_decisions.items():
            records = by_action_id[op_id]
            assert len(records) == 2
            assert records[1]["agent_audit.decision"] == decision
            if decision in {"deny", "timeout", "cancel", "auto_deny"}:
                assert records[1]["agent_audit.cost.wasted"] is True

        expected_outcomes = {
            "op-T11": ("not_executed", "expired"),
            "op-T12": ("not_executed", "cancelled"),
            "op-T13": ("success", None),
            "op-T14": ("failure", None),
            "op-T15": ("failure", None),
        }
        for op_id, (outcome, reason) in expected_outcomes.items():
            records = by_action_id[op_id]
            assert len(records) == 3
            executed = records[2]
            assert executed["agent_audit.outcome"] == outcome
            if reason is not None:
                assert executed["agent_audit.not_executed_reason"] == reason

    def test_denial_never_produces_an_executed_record(self) -> None:
        from agent_audit_record import Emitter

        cap, provider = self._capture()
        agent_audit._EMITTER = Emitter(logger_provider=provider)

        agent_audit.emit_proposed(
            operation_id="op-deny", principal_id="p1", environment="default", workflow_id="wf1"
        )
        agent_audit.emit_transition("T07", operation_id="op-deny", actor="approver")

        assert cap.event_names == ["agent_audit.proposed", "agent_audit.decided"]
