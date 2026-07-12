import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "hermes_cli" / "codex_priority_sync.py"


def load_module():
    spec = importlib.util.spec_from_file_location("codex_reset_aware_priority_sync_test", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_profile_home_controls_auth_and_state_paths(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    module = load_module()

    assert module.HERMES_HOME == profile_home
    assert module.HERMES_AUTH == profile_home / "auth.json"
    assert module.STATE_PATH == profile_home / "state" / "codex_reset_aware_warmup.json"


def test_fixed_route_policy_overrides_reset_aware_recommendation(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    module = load_module()
    policy_path = tmp_path / "global-state" / "codex_route_policy.json"
    policy_path.parent.mkdir()
    policy_path.write_text(
        json.dumps({"mode": "fixed", "credential_id": "personal-id", "label": "personal-backup"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROUTE_POLICY_PATH", policy_path)
    payload = {
        "accounts": [
            {"credential_id": "company-id", "label": "company-plus-100", "ok": True},
            {"credential_id": "personal-id", "label": "openai-codex-oauth-1", "ok": True},
        ],
        "recommendation": {"label": "company-plus-100", "policy": "7d-reset-aware"},
    }

    recommendation = module.choose_route_recommendation(payload)

    assert recommendation == {
        "label": "openai-codex-oauth-1",
        "credential_id": "personal-id",
        "reason": "manual fixed route",
        "policy": "fixed",
    }


def test_main_applies_fixed_route_policy(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    module = load_module()
    monkeypatch.setenv("HERMES_CODEX_ROUTE_LOCK_HELD", "1")
    policy_path = tmp_path / "codex_route_policy.json"
    policy_path.write_text(
        json.dumps({"mode": "fixed", "credential_id": "personal-id", "label": "personal-backup"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROUTE_POLICY_PATH", policy_path)
    monkeypatch.setattr(
        module,
        "collect_payload",
        lambda: {
            "accounts": [
                {"credential_id": "company-id", "label": "company-plus-100", "ok": True},
                {"credential_id": "personal-id", "label": "personal-backup", "ok": True},
            ],
            "recommendation": {"label": "company-plus-100", "policy": "7d-reset-aware"},
        },
    )
    monkeypatch.setattr(module, "load_state", lambda: {})
    selected = []
    monkeypatch.setattr(module, "sync_hermes", lambda label, dry_run: selected.append(label) or [])

    result = module.main(["--dry-run", "--skip-cliproxy"])

    assert result == 0
    assert selected == ["personal-backup"]


def test_routine_priority_changes_are_silent_without_report(monkeypatch, tmp_path, capsys):
    """The 15-minute auto-sync must not page Telegram for expected route churn."""
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    module = load_module()
    monkeypatch.setenv("HERMES_CODEX_ROUTE_LOCK_HELD", "1")
    monkeypatch.setattr(
        module,
        "collect_payload",
        lambda: {
            "recommendation": {
                "label": "company-plus-100",
                "policy": "7d-reset-aware",
                "reason": "quota availability changed",
            },
            "accounts": [],
        },
    )
    monkeypatch.setattr(module, "load_state", lambda: {})
    monkeypatch.setattr(module, "sync_hermes", lambda *_args, **_kwargs: ["Hermes personal: priority 0 -> 10"])
    monkeypatch.setattr(module, "sync_cliproxy", lambda *_args, **_kwargs: ["CLIProxyAPI personal: priority 100 -> 0"])
    monkeypatch.setattr(module, "sync_native_codex", lambda *_args, **_kwargs: ["Native Codex CLI -> company"])

    assert module.main([]) == 0
    assert capsys.readouterr().out == ""


def test_sync_native_codex_activates_matching_native_account(monkeypatch, tmp_path):
    module = load_module()
    command = tmp_path / "codex-account"
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(module, "NATIVE_CODEX_ACCOUNT", command)
    calls = []

    class Result:
        returncode = 0
        stdout = "CHANGED\n"
        stderr = ""

    monkeypatch.setattr(module.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)) or Result())

    changes = module.sync_native_codex("company-plus-100", dry_run=False)

    assert changes == ["Native Codex CLI -> company"]
    assert calls[0][0][-2:] == ["activate", "company"]
    assert calls[0][1]["shell"] is False


def test_sync_hermes_updates_only_active_profile_auth(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    auth_path = profile_home / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {"label": "company-plus-100", "priority": 0},
                        {"label": "personal-backup", "priority": 2},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    module = load_module()

    changes = module.sync_hermes("personal-backup", dry_run=False)
    saved = json.loads(auth_path.read_text(encoding="utf-8"))
    rows = saved["credential_pool"]["openai-codex"]

    assert changes
    assert [(row["label"], row["priority"]) for row in rows] == [
        ("company-plus-100", 10),
        ("personal-backup", 0),
    ]
    assert auth_path.stat().st_mode & 0o777 == 0o600
