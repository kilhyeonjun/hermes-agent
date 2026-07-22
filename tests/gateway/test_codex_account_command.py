import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, PickerCallbackOutcome
from gateway.session import SessionSource


def _runner():
    from gateway.run import GatewayRunner

    return object.__new__(GatewayRunner)


class _AccountPickerAdapter:
    def __init__(self):
        self.kwargs = None

    async def send_codex_account_picker(self, **kwargs):
        self.kwargs = kwargs
        return type("Result", (), {"success": True})()


def _picker_event():
    return MessageEvent(
        text="/codex_account",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="user-1",
        ),
    )


@pytest.fixture(autouse=True)
def _isolated_native_account_script(tmp_path, monkeypatch):
    hermes_root = tmp_path / "hermes-root"
    script = hermes_root / "scripts" / "codex_native_account.py"
    script.parent.mkdir(parents=True)
    script.write_text("# test-only native account control\n", encoding="utf-8")
    script.chmod(0o700)
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    return script


def test_codex_account_command_registered_with_telegram_alias():
    from hermes_cli.commands import resolve_command

    command = resolve_command("codex_account")

    assert command is not None
    assert command.name == "codex-account"
    assert command.args_hint == "[status|auto|personal|company]"
    assert command.gateway_only is True


@pytest.mark.asyncio
async def test_codex_account_controls_macmini_only_and_warns_about_running_sessions(
    _isolated_native_account_script,
):
    runner = _runner()
    event = MagicMock()
    event.get_command_args.return_value = "company"
    local = subprocess.CompletedProcess(args=[], returncode=0, stdout="Current: company\n", stderr="")

    with patch("subprocess.run", return_value=local) as run:
        result = await runner._handle_codex_account_command(event)

    assert "Mac mini" in result
    assert "MacBook" not in result
    assert "Current: company" in result
    assert "실행 중인 Codex" in result
    assert "resume --last" in result
    assert run.call_count == 1
    argv = run.call_args.args[0]
    assert argv[-2:] == [
        str(_isolated_native_account_script),
        "company",
    ]
    assert "hermes_cli.codex_route" not in argv
    assert argv[0] != "ssh"
    assert run.call_args.kwargs["shell"] is False
    from hermes_cli.codex_route import ROUTE_COMMAND_TIMEOUT
    assert run.call_args.kwargs["timeout"] == ROUTE_COMMAND_TIMEOUT


@pytest.mark.asyncio
async def test_codex_account_without_args_opens_native_account_picker():
    runner = _runner()
    adapter = _AccountPickerAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    status = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="Codex account mode: auto\nNative Codex CLI: personal\n", stderr=""
    )
    switched = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="CHANGED\nNative Codex CLI: company\n", stderr=""
    )

    with patch("subprocess.run", side_effect=[status, switched]) as run:
        result = await runner._handle_codex_account_command(_picker_event())
        response = await adapter.kwargs["on_selected"]("company")

    assert result is None
    assert adapter.kwargs["current_account"] == "personal"
    assert adapter.kwargs["current_mode"] == "auto"
    assert adapter.kwargs["owner_user_id"] == "user-1"
    assert "Native Codex CLI: company" in response
    assert isinstance(response, PickerCallbackOutcome)
    assert response.status == "success"
    assert run.call_count == 2


@pytest.mark.asyncio
async def test_codex_account_picker_callback_preserves_child_failure_status():
    runner = _runner()
    adapter = _AccountPickerAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    status = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="Codex account mode: auto\nNative Codex CLI: personal\n", stderr=""
    )
    failed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="switch failed\n"
    )

    with patch("subprocess.run", side_effect=[status, failed]):
        assert await runner._handle_codex_account_command(_picker_event()) is None
        outcome = await adapter.kwargs["on_selected"]("company")

    assert isinstance(outcome, PickerCallbackOutcome)
    assert outcome.status == "failure"
    assert "switch failed" in outcome


@pytest.mark.asyncio
async def test_codex_account_status_failure_does_not_open_picker():
    runner = _runner()
    adapter = _AccountPickerAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    failed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="status failed"
    )

    with patch("subprocess.run", return_value=failed):
        result = await runner._handle_codex_account_command(_picker_event())

    assert "❌ Mac mini" in result
    assert adapter.kwargs is None


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [0, 1])
async def test_codex_account_force_redacts_child_output(returncode):
    from agent import redact

    runner = _runner()
    event = MagicMock()
    event.get_command_args.return_value = "company"
    secret = "sk" + "-proj-" + ("A" * 40)
    auth_header = "Author" + "ization: Bearer "
    token_field = "access_" + "token"
    diagnostic = (
        f"account {'applied' if returncode == 0 else 'failed'}\n"
        f"{auth_header}{secret}\n"
        f'{{"{token_field}":"{secret}"}}'
    )
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=diagnostic if returncode == 0 else "",
        stderr=diagnostic if returncode else "",
    )

    with patch.object(redact, "_REDACT_ENABLED", False), patch(
        "subprocess.run", return_value=completed
    ):
        result = await runner._handle_codex_account_command(event)

    assert secret not in result
    assert ("applied" if returncode == 0 else "failed") in result


@pytest.mark.asyncio
async def test_codex_account_force_redacts_launch_exception():
    from agent import redact

    runner = _runner()
    event = MagicMock()
    event.get_command_args.return_value = "company"
    secret = "sk" + "-proj-" + ("B" * 40)
    failure = OSError(("Author" + "ization: Bearer ") + secret)

    with patch.object(redact, "_REDACT_ENABLED", False), patch(
        "subprocess.run", side_effect=failure
    ):
        result = await runner._handle_codex_account_command(event)

    assert secret not in result
    assert "Codex 계정 명령 실패" in result


@pytest.mark.asyncio
async def test_codex_account_rejects_host_target_without_subprocess():
    runner = _runner()
    event = MagicMock()
    event.get_command_args.return_value = "status macbook"

    with patch("subprocess.run") as run:
        result = await runner._handle_codex_account_command(event)

    run.assert_not_called()
    assert "사용법" in result
    assert "Mac mini만" in result


@pytest.mark.asyncio
async def test_codex_account_rejects_unknown_mode_without_subprocess():
    runner = _runner()
    event = MagicMock()
    event.get_command_args.return_value = "anything;rm all"

    with patch("subprocess.run") as run:
        result = await runner._handle_codex_account_command(event)

    run.assert_not_called()
    assert "사용법" in result
    assert "/codex_account auto" in result


@pytest.mark.asyncio
async def test_codex_account_reports_missing_isolated_control_script(
    tmp_path, monkeypatch,
):
    empty_root = tmp_path / "empty-hermes-root"
    monkeypatch.setenv("HERMES_HOME", str(empty_root))
    runner = _runner()
    event = MagicMock()
    event.get_command_args.return_value = "company"

    with patch("subprocess.run") as run:
        result = await runner._handle_codex_account_command(event)

    run.assert_not_called()
    assert "스크립트를 찾지 못했습니다" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_kind", ["symlink", "group_writable", "hardlink"])
async def test_codex_account_rejects_untrusted_control_script(
    _isolated_native_account_script, unsafe_kind,
):
    script = _isolated_native_account_script
    if unsafe_kind == "symlink":
        target = script.with_name("real_native_account.py")
        target.write_text("# target\n", encoding="utf-8")
        target.chmod(0o700)
        script.unlink()
        script.symlink_to(target)
    elif unsafe_kind == "group_writable":
        script.chmod(0o720)
    else:
        target = script.with_name("hardlinked_native_account.py")
        target.write_text("# target\n", encoding="utf-8")
        target.chmod(0o700)
        script.unlink()
        script.hardlink_to(target)

    runner = _runner()
    event = MagicMock()
    event.get_command_args.return_value = "company"
    with patch("subprocess.run") as run:
        result = await runner._handle_codex_account_command(event)

    run.assert_not_called()
    assert "신뢰할 수 없습니다" in result


@pytest.mark.asyncio
async def test_codex_account_fails_closed_without_posix_owner_check(
    _isolated_native_account_script, monkeypatch,
):
    monkeypatch.delattr(os, "geteuid")
    runner = _runner()
    event = MagicMock()
    event.get_command_args.return_value = "company"

    with patch("subprocess.run") as run:
        result = await runner._handle_codex_account_command(event)

    run.assert_not_called()
    assert result is not None
    assert "POSIX 소유권 검사를 지원하지 않습니다" in result
