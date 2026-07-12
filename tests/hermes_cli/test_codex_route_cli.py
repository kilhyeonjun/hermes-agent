import subprocess
import sys
import types


def test_codex_route_module_runs_control_script_without_shell(monkeypatch, tmp_path, capsys):
    from hermes_cli import codex_route

    script = tmp_path / "codex_route_control.py"
    script.write_text("# test", encoding="utf-8")
    monkeypatch.setattr(codex_route, "CONTROL_SCRIPT", script)
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="mode=company\n", stderr=""
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return completed

    monkeypatch.setattr(codex_route.subprocess, "run", fake_run)

    result = codex_route.main(["company"])

    assert result == 0
    assert capsys.readouterr().out == "mode=company\n"
    argv, kwargs = calls[0]
    assert argv == [sys.executable, str(script), "company"]
    assert kwargs["shell"] is False


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
