import subprocess
from unittest.mock import MagicMock, patch

import pytest


def _runner():
    from gateway.run import GatewayRunner

    return object.__new__(GatewayRunner)


def test_codex_account_command_registered_with_telegram_alias():
    from hermes_cli.commands import resolve_command

    command = resolve_command("codex_account")

    assert command is not None
    assert command.name == "codex-account"
    assert command.args_hint == "[status|auto|personal|company]"
    assert command.gateway_only is True


@pytest.mark.asyncio
async def test_codex_account_controls_macmini_only_and_warns_about_running_sessions():
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
    assert argv[-3:] == ["-m", "hermes_cli.codex_route", "company"]
    assert argv[0] != "ssh"
    assert run.call_args.kwargs["shell"] is False
    from hermes_cli.codex_route import ROUTE_COMMAND_TIMEOUT
    assert run.call_args.kwargs["timeout"] == ROUTE_COMMAND_TIMEOUT


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
