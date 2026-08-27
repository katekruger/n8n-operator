"""Structural checks on the live-n8n harness itself (docker/live-n8n/,
scripts/live_n8n_up.sh, scripts/live_n8n_down.sh) — everything that can be verified
without Docker or a real n8n instance, so a broken harness is caught by the normal
(non-live) test suite rather than only discovered the next time someone actually runs
it (BUILD_PLAN section 12, phase 9 continuation: live-n8n reproducibility).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from n8n_operator.registry.schema import Trigger, WorkflowEntry

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker" / "live-n8n" / "docker-compose.yml"
UP_SCRIPT = REPO_ROOT / "scripts" / "live_n8n_up.sh"
DOWN_SCRIPT = REPO_ROOT / "scripts" / "live_n8n_down.sh"


@pytest.mark.unit
def test_compose_file_exists_and_is_valid_yaml() -> None:
    assert COMPOSE_FILE.is_file()
    document = yaml.safe_load(COMPOSE_FILE.read_text())
    assert "services" in document
    assert "n8n" in document["services"]


@pytest.mark.unit
def test_compose_file_pins_the_compatibility_matrix_version() -> None:
    """The image tag must match docs/COMPATIBILITY_MATRIX.md's tested version — never
    silently drift to `latest` or an unpinned tag."""
    document = yaml.safe_load(COMPOSE_FILE.read_text())
    image = document["services"]["n8n"]["image"]
    assert image.endswith(":2.35.7"), (
        f"docker-compose.yml pins {image!r}; update it and "
        "docs/COMPATIBILITY_MATRIX.md together, on real evidence (ADR-008)"
    )


@pytest.mark.unit
def test_compose_file_binds_only_to_loopback() -> None:
    document = yaml.safe_load(COMPOSE_FILE.read_text())
    ports = document["services"]["n8n"]["ports"]
    assert len(ports) == 1
    assert ports[0].startswith("127.0.0.1:"), (
        "the live-n8n test instance must never bind beyond loopback"
    )


@pytest.mark.unit
def test_compose_file_uses_a_dedicated_named_volume_not_a_bind_mount() -> None:
    document = yaml.safe_load(COMPOSE_FILE.read_text())
    volumes = document["services"]["n8n"]["volumes"]
    assert len(volumes) == 1
    assert volumes[0].split(":")[0] == "n8n_live_test_data"
    assert "n8n_live_test_data" in document["volumes"]
    # A named volume, not a host path — nothing here reads or writes a path under the
    # operator repository itself.
    assert not volumes[0].startswith((".", "/", "~"))


@pytest.mark.unit
def test_compose_file_declares_a_healthcheck() -> None:
    document = yaml.safe_load(COMPOSE_FILE.read_text())
    assert "healthcheck" in document["services"]["n8n"]


@pytest.mark.unit
def test_compose_project_is_named_for_scoped_teardown() -> None:
    """`docker compose down` must only ever remove resources under this project's own
    name — never another container/volume on the host."""
    document = yaml.safe_load(COMPOSE_FILE.read_text())
    assert document.get("name") == "n8n-operator-live-test"


@pytest.mark.unit
@pytest.mark.parametrize("script", [UP_SCRIPT, DOWN_SCRIPT])
def test_harness_scripts_exist_and_are_executable(script: Path) -> None:
    assert script.is_file()
    assert script.stat().st_mode & 0o111, f"{script} is not executable"


@pytest.mark.unit
@pytest.mark.parametrize("script", [UP_SCRIPT, DOWN_SCRIPT])
def test_harness_scripts_are_syntactically_valid_bash(script: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_down_script_never_touches_resources_outside_its_own_compose_file() -> None:
    """A cheap but meaningful guard against a future edit accidentally widening the
    teardown to something unscoped (e.g. a bare `docker system prune`)."""
    text = DOWN_SCRIPT.read_text()
    assert "docker compose -f" in text
    assert "system prune" not in text
    assert "docker rm" not in text  # only ever through compose, never a bare container rm


@pytest.mark.unit
def test_synthetic_workflow_entry_shape_matches_the_registry_schema() -> None:
    """The exact ``WorkflowEntry`` construction ``tests/live/test_live_n8n.py`` builds
    for the drift-detection checks, proven valid against the real schema without
    needing a live instance or any ``N8N_LIVE_*`` variable — a typo in a field name
    here should fail on every CI run, not only the next live-n8n run."""
    entry = WorkflowEntry(
        id="live.synthetic_test_workflow",
        n8n_workflow_id="wf_fake_for_schema_check",
        title="n8n Operator — synthetic test workflow",
        description="Live compatibility harness target.",
        owner="live-n8n-harness",
        version=1,
        definition_hash="sha256:" + "0" * 64,
        risk="low",
        side_effects="read_only",
        approval="none",
        trigger=Trigger(
            type="webhook",
            method="POST",
            path="/webhook/operator-smoke-test",
            auth="none",
            correlation="response_envelope",
        ),
        input_schema={"type": "object", "additionalProperties": True},
    )
    assert entry.trigger.path == "/webhook/operator-smoke-test"
    assert entry.trigger.correlation == "response_envelope"
