import json
import subprocess
import sys
import types

import pytest


def test_resolve_policy_records_exact_canonical_kind(tmp_path):
    from hermes_cli import codex_route

    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"credential_pool":{"openai-codex":['
        '{"id":"company-id","label":"company-plus-100"}]}}',
        encoding="utf-8",
    )

    policy = codex_route.resolve_policy("company", auth)

    assert policy == {
        "mode": "fixed",
        "credential_id": "company-id",
        "label": "company-plus-100",
        "kind": "company",
    }


@pytest.mark.parametrize(
    ("mode", "label"),
    [
        ("company", "personal-company-plus"),
        ("personal", "unclassified-account"),
    ],
)
def test_resolve_policy_rejects_ambiguous_or_unknown_labels(tmp_path, mode, label):
    from hermes_cli import codex_route

    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {"id": "candidate-id", "label": label},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="계정을 찾지 못했습니다"):
        codex_route.resolve_policy(mode, auth)


def test_resolve_policy_rejects_duplicate_id_anywhere_in_pool(tmp_path):
    from hermes_cli import codex_route

    private_label = "private-seat-owner@example.invalid"
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "shared-id",
                            "label": "company-plus-100",
                        },
                        {
                            "id": "shared-id",
                            "label": private_label,
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc:
        codex_route.resolve_policy("company", auth)

    assert "credential ID" in str(exc.value)
    assert private_label not in str(exc.value)


def test_load_policy_missing_is_auto_but_corrupt_or_unknown_is_rejected(
    monkeypatch, tmp_path
):
    from hermes_cli import codex_route

    policy_path = tmp_path / "codex_route_policy.json"
    monkeypatch.setattr(codex_route, "POLICY_PATH", policy_path)
    assert codex_route.load_policy() == {"mode": "auto"}

    policy_path.write_text('{"mode":', encoding="utf-8")
    with pytest.raises(ValueError, match="policy is invalid"):
        codex_route.load_policy()

    policy_path.write_text('{"mode":"surprise"}', encoding="utf-8")
    with pytest.raises(ValueError, match="policy is invalid"):
        codex_route.load_policy()


def test_canonical_row_label_never_echoes_unknown_account_metadata(
    monkeypatch, tmp_path
):
    from hermes_cli import codex_route

    secret_label = "private-seat-owner@example.invalid"
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {"id": "unknown-id", "label": secret_label},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_route, "DEFAULT_AUTH", auth)

    label = codex_route.canonical_row_label(
        {"id": "unknown-id", "label": secret_label}
    )

    assert label == "unknown"
    assert secret_label not in label


def test_codex_route_status_works_without_external_control_script(monkeypatch, tmp_path, capsys):
    from hermes_cli import codex_route

    hermes_home = tmp_path / "clean-home" / ".hermes"
    monkeypatch.setattr(codex_route, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(codex_route, "DEFAULT_AUTH", hermes_home / "auth.json")
    monkeypatch.setattr(codex_route, "POLICY_PATH", hermes_home / "state" / "codex_route_policy.json")
    monkeypatch.setattr(codex_route, "CLIPROXY_AUTH_DIR", tmp_path / "missing-cliproxy")
    monkeypatch.setattr(codex_route, "NATIVE_CODEX_AUTH", tmp_path / "missing-codex" / "auth.json")

    result = codex_route.main(["status"])

    assert result == 0
    output = capsys.readouterr().out
    assert "Codex 라우팅" in output
    assert "제어 스크립트" not in output


def test_auto_sync_splits_default_personal_from_global_clients(monkeypatch, tmp_path):
    from hermes_cli import codex_route

    homes = [("default", tmp_path / "default"), ("gameduo", tmp_path / "gameduo")]
    monkeypatch.setattr(codex_route, "profile_homes", lambda: homes)
    monkeypatch.setattr(codex_route, "load_policy", lambda: {"mode": "auto"})
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(codex_route.subprocess, "run", fake_run)

    assert codex_route.sync_all_profiles() == []
    assert len(calls) == 3
    commands = sorted((tuple(argv[3:]), kwargs["env"]["HERMES_HOME"]) for argv, kwargs in calls)
    assert commands == sorted(
        [
            (("--profile-account", "personal", "--skip-cliproxy"), str(homes[0][1])),
            (("--skip-hermes",), str(homes[0][1])),
            (("--skip-cliproxy",), str(homes[1][1])),
        ]
    )
    for argv, kwargs in calls:
        assert argv[:3] == [sys.executable, "-m", "hermes_cli.codex_priority_sync"]
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == codex_route.PROFILE_SYNC_TIMEOUT
        assert kwargs["env"]["HERMES_CODEX_ROUTE_LOCK_HELD"] == "1"


def test_fixed_sync_keeps_default_and_global_clients_in_one_transaction(
    monkeypatch, tmp_path
):
    from hermes_cli import codex_route

    homes = [("default", tmp_path / "default"), ("gameduo", tmp_path / "gameduo")]
    monkeypatch.setattr(codex_route, "profile_homes", lambda: homes)
    monkeypatch.setattr(
        codex_route,
        "load_policy",
        lambda: {"mode": "fixed", "credential_id": "company-id"},
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(codex_route.subprocess, "run", fake_run)

    assert codex_route.sync_all_profiles() == []
    commands = sorted((tuple(argv[3:]), kwargs["env"]["HERMES_HOME"]) for argv, kwargs in calls)
    assert commands == sorted(
        [
            ((), str(homes[0][1])),
            (("--skip-cliproxy",), str(homes[1][1])),
        ]
    )


def test_profile_sync_failure_redacts_child_output(monkeypatch, tmp_path):
    from hermes_cli import codex_route

    secret = "sk" + "-" + "abcdefghijklmnopqrstuv"
    auth_header = "Author" + "ization: Bearer "
    token_field = "access_" + "token"
    result = subprocess.CompletedProcess(
        ["python", "-m", "hermes_cli.codex_priority_sync"],
        1,
        "",
        f'{auth_header}{secret}\n{{"{token_field}":"{secret}"}}\n',
    )
    monkeypatch.setattr(codex_route.subprocess, "run", lambda *_args, **_kwargs: result)

    detail = codex_route._sync_profile("gameduo", tmp_path / "gameduo")

    assert detail is not None
    assert secret not in detail
    assert detail.startswith("gameduo: " + auth_header)


def test_profile_sync_launch_error_is_force_redacted(monkeypatch, tmp_path):
    from agent import redact
    from hermes_cli import codex_route

    secret = "sk" + "-proj-" + ("Z" * 40)
    failure = OSError(("Author" + "ization: Bearer ") + secret)

    def fail_run(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(codex_route.subprocess, "run", fail_run)
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)

    detail = codex_route._sync_profile("gameduo", tmp_path / "gameduo")

    assert secret not in detail
    assert detail.startswith("gameduo: sync launch failed: ")


def test_partial_profile_failure_restores_policy_and_attempts_full_rollback(monkeypatch, tmp_path, capsys):
    from hermes_cli import codex_route

    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"credential_pool": {"openai-codex": ['
        '{"id": "personal-id", "label": "personal-backup"},'
        '{"id": "company-id", "label": "company-plus-100"}]}}',
        encoding="utf-8",
    )
    policy = tmp_path / "state" / "codex_route_policy.json"
    policy.parent.mkdir()
    original = '{"mode": "auto", "owner": "before"}\n'
    policy.write_text(original, encoding="utf-8")
    monkeypatch.setattr(codex_route, "DEFAULT_AUTH", auth)
    monkeypatch.setattr(codex_route, "POLICY_PATH", policy)
    rounds = iter([["gameduo: sync failed"], ["penguincouple: rollback failed"]])
    calls = []

    def fake_sync():
        calls.append(policy.read_text(encoding="utf-8"))
        return next(rounds)

    monkeypatch.setattr(codex_route, "sync_all_profiles", fake_sync)

    result = codex_route.main(["company"])

    assert result != 0
    assert len(calls) == 2
    assert '"mode": "fixed"' in calls[0]
    assert calls[1] == original
    assert policy.read_text(encoding="utf-8") == original
    output = capsys.readouterr().out
    assert "gameduo: sync failed" in output
    assert "penguincouple: rollback failed" in output
    assert "rollback" in output.lower()


def test_unexpected_sync_exception_still_restores_and_runs_rollback(
    monkeypatch, tmp_path, capsys
):
    from hermes_cli import codex_route

    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"credential_pool": {"openai-codex": ['
        '{"id": "company-id", "label": "company-plus-100"}]}}',
        encoding="utf-8",
    )
    policy = tmp_path / "state" / "codex_route_policy.json"
    policy.parent.mkdir()
    original = b'{"mode": "auto"}\n'
    policy.write_bytes(original)
    monkeypatch.setattr(codex_route, "DEFAULT_AUTH", auth)
    monkeypatch.setattr(codex_route, "POLICY_PATH", policy)
    calls = 0

    def fake_sync():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unexpected profile discovery failure")
        return []

    monkeypatch.setattr(codex_route, "sync_all_profiles", fake_sync)

    assert codex_route.main(["company"]) == 1
    assert calls == 2
    assert policy.read_bytes() == original
    output = capsys.readouterr().out
    assert "profile sync raised RuntimeError" in output
    assert "rollback 완료" in output


def test_top_level_codex_route_dispatches_mode_to_handler(monkeypatch):
    import hermes_cli.main as main_mod

    calls = []
    fake_module = types.ModuleType("hermes_cli.codex_route")
    setattr(fake_module, "main", lambda argv=None: calls.append(argv) or 0)
    monkeypatch.setitem(sys.modules, "hermes_cli.codex_route", fake_module)
    monkeypatch.setattr(sys, "argv", ["hermes", "codex-route", "company"])
    monkeypatch.setattr("hermes_cli.config.get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_prepare_agent_startup", lambda _args: None)

    main_mod.main()

    assert calls == [["company"]]
