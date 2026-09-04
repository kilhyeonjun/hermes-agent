"""Root-owned runtime contracts are stable prompt content for every profile."""

from pathlib import Path
from unittest.mock import patch

from agent.system_prompt import build_system_prompt_parts
from tests.agent.test_system_prompt import _make_agent


def test_runtime_contracts_load_packaged_defaults_and_root_overrides(monkeypatch, tmp_path):
    root = tmp_path / "root"
    profile = root / "profiles" / "coder"
    monkeypatch.setenv("HERMES_HOME", str(profile))
    with patch("agent.system_prompt.get_default_hermes_root", return_value=root):
        stable = build_system_prompt_parts(_make_agent(skip_context_files=True))["stable"]
    assert "PERFORMANCE_OBSERVATION_CONTRACT_V1" in stable
    assert "OUTCOME_CONTROL_V1" in stable
    assert not (profile / "runtime-contracts").exists()

    contracts = root / "runtime-contracts"
    contracts.mkdir(parents=True)
    for name in ("performance-observation.md", "outcome-control.md"):
        (contracts / name).write_text(f"{name} root override", encoding="utf-8")
    with patch("agent.system_prompt.get_default_hermes_root", return_value=root):
        overridden = build_system_prompt_parts(_make_agent(skip_context_files=True))["stable"]
    assert "performance-observation.md root override" in overridden
    assert "outcome-control.md root override" in overridden


def test_packaged_runtime_contracts_are_complete():
    root = Path(__file__).parents[2] / "agent" / "runtime_contracts"
    combined = "\n".join(path.read_text() for path in root.glob("*.md"))
    for phrase in (
        "PERFORMANCE_OBSERVATION_CONTRACT_V1",
        "OUTCOME_CONTROL_V1",
        "repository or tool budgets first",
        "more than 20% and at least 1 second",
        "at least 20% of total and at least 10 seconds",
        "repeatedly takes more than 30 seconds",
        "Non-blocking",
        "One review, one re-review, at most one runtime retry",
        "A timeout never approves",
        "REMOTE_DELIVERY_GATE",
    ):
        assert phrase in combined