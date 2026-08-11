"""Root runtime contracts are stable prompt content for every profile."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.system_prompt import build_system_prompt_parts


def _agent():
    return SimpleNamespace(
        load_soul_identity=False, skip_context_files=True, valid_tool_names=[],
        _task_completion_guidance=False, _tool_use_enforcement=False,
        _environment_probe=False, _kanban_worker_guidance="", _memory_store=None,
        _memory_manager=None, model="", provider="", platform="", pass_session_id=False,
        session_id="",
    )


def test_runtime_contracts_load_from_packaged_defaults_in_a_clean_home(monkeypatch, tmp_path):
    clean_home = tmp_path / "clean-home" / "profiles" / "coder"
    monkeypatch.setenv("HERMES_HOME", str(clean_home))
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        stable = build_system_prompt_parts(_agent())["stable"]
    assert not (clean_home / "runtime-contracts").exists()
    assert stable.count("PERFORMANCE_OBSERVATION_CONTRACT_V1") == 1
    assert stable.count("OUTCOME_CONTROL_V1") == 1


def test_runtime_contract_root_override_wins_when_present(monkeypatch, tmp_path):
    root = tmp_path / "root"
    contracts = root / "runtime-contracts"
    contracts.mkdir(parents=True)
    (contracts / "performance-observation.md").write_text("# PERFORMANCE_OBSERVATION_CONTRACT_V1\noverride")
    (contracts / "outcome-control.md").write_text("# OUTCOME_CONTROL_V1\noverride")
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "coder"))
    with (
        patch("agent.prompt_builder.get_default_hermes_root", return_value=root),
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        stable = build_system_prompt_parts(_agent())["stable"]
    assert stable.count("override") == 2


def test_packaged_runtime_contracts_are_present_and_complete():
    root = Path(__file__).parents[2] / "agent" / "runtime_contracts"
    performance = (root / "performance-observation.md").read_text()
    outcome = (root / "outcome-control.md").read_text()
    for phrase in (
        "commands, tool calls, agent tasks, builds, tests, deployments, browser work, and network operations",
        "stage or bottleneck signals",
        "repository or tool budgets first",
        "more than 20% and at least 1 second",
        "at least 20% of total and at least 10 seconds",
        "repeatedly takes more than 30 seconds",
        "actionable",
        "evidence, root cause, options, exactly one fundamental recommendation",
        "obtain approval before optimizing",
        "unavailable required evidence, and skipped inspection",
    ):
        assert phrase in performance
    for phrase in (
        "evidence-backed blockers",
        "Non-blocking",
        "3 derived tasks, 15m, 2 reviewer dispatches, second repo, or second full-suite run",
        "One review, one re-review, at most one runtime retry",
        "polling expiry while runtime status is running is non-terminal",
        "runtime-declared retryable terminal transport or execution failure",
        "REVIEW_UNAVAILABLE",
        "timeout never approves",
        "Plans stay within outcome",
        "fetch; local HEAD = fetched remote branch SHA",
    ):
        assert phrase in outcome
