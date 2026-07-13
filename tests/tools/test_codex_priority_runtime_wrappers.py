from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def test_runtime_python_entrypoint_is_only_a_canonical_package_shim():
    source = (SCRIPTS / "codex_reset_aware_priority_sync.py").read_text()

    assert "from hermes_cli.codex_priority_sync import main" in source
    assert "collect_payload" not in source
    assert "run_warmup_call" not in source


def test_recurring_shell_wrappers_use_package_and_never_auto_warmup():
    shared = (SCRIPTS / "codex_reset_aware_priority_sync.sh").read_text()
    default = (
        SCRIPTS / "codex_default_personal_priority_sync.sh"
    ).read_text()

    assert "-m hermes_cli.codex_priority_sync" in shared
    assert "--warmup-unstarted" not in shared
    assert "--warmup-unstarted" not in default
    assert "--profile-account personal" in default
    assert "--skip-cliproxy" in default
