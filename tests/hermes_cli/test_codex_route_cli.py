import subprocess
import sys
import types


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


def test_sync_all_profiles_invokes_tracked_module_without_shell(monkeypatch, tmp_path):
    from hermes_cli import codex_route

    homes = [("default", tmp_path / "default"), ("gameduo", tmp_path / "gameduo")]
    monkeypatch.setattr(codex_route, "profile_homes", lambda: homes)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(codex_route.subprocess, "run", fake_run)

    assert codex_route.sync_all_profiles() == []
    assert len(calls) == 2
    by_home = {kwargs["env"]["HERMES_HOME"]: (argv, kwargs) for argv, kwargs in calls}
    for name, home in homes:
        argv, kwargs = by_home[str(home)]
        assert argv[:3] == [sys.executable, "-m", "hermes_cli.codex_priority_sync"]
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == codex_route.PROFILE_SYNC_TIMEOUT
        assert kwargs["env"]["HERMES_CODEX_ROUTE_LOCK_HELD"] == "1"
        assert ("--skip-cliproxy" in argv) is (name != "default")


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
