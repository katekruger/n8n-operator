"""Client examples stay safe and aligned with shipped transport controls."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_openai_responses_example_has_both_remote_transport_guards() -> None:
    example = json.loads(
        (REPO_ROOT / "examples/mcp-clients/openai_responses_tool.json").read_text()
    )

    assert example["type"] == "mcp"
    assert example["server_url"].startswith("https://")
    assert example["headers"]["Authorization"].startswith("Bearer REPLACE_")
    assert example["headers"]["Origin"].startswith("https://")
    assert example["require_approval"] in {"always", "never"}


def test_client_examples_contain_placeholders_not_live_credentials() -> None:
    examples = REPO_ROOT / "examples/mcp-clients"
    for path in examples.glob("*.json"):
        text = path.read_text()
        assert "sk-" not in text
        assert "REPLACE_WITH" in text or path.name == "claude_desktop_config.json"
