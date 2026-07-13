import subprocess
from unittest.mock import MagicMock, patch

import pytest


def _runner():
    from gateway.run import GatewayRunner

    return object.__new__(GatewayRunner)


@pytest.mark.asyncio
async def test_codex_route_handler_runs_control_script_with_valid_mode():
    runner = _runner()
    event = MagicMock()
    event.get_command_args.return_value = "personal"
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="🎛 Codex 라우팅\n모드: 개인 고정\n", stderr="")

    with patch("subprocess.run", return_value=completed) as run:
        result = await runner._handle_codex_route_command(event)

    assert "개인 고정" in result
    argv = run.call_args.args[0]
    assert argv[-3:] == ["-m", "hermes_cli.codex_route", "personal"]
    assert run.call_args.kwargs["shell"] is False
    from hermes_cli.codex_route import ROUTE_COMMAND_TIMEOUT
    assert run.call_args.kwargs["timeout"] == ROUTE_COMMAND_TIMEOUT


@pytest.mark.asyncio
async def test_codex_route_handler_rejects_unknown_mode_without_subprocess():
    runner = _runner()
    event = MagicMock()
    event.get_command_args.return_value = "anything;rm"

    with patch("subprocess.run") as run:
        result = await runner._handle_codex_route_command(event)

    run.assert_not_called()
    assert "사용법" in result
    assert "/codex_route auto" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [0, 1])
async def test_codex_route_handler_force_redacts_child_output(returncode):
    from agent import redact

    runner = _runner()
    event = MagicMock()
    event.get_command_args.return_value = "company"
    secret = "sk" + "-proj-" + ("X" * 40)
    auth_header = "Author" + "ization: Bearer "
    token_field = "access_" + "token"
    diagnostic = (
        f"route {'applied' if returncode == 0 else 'failed'}\n"
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
        result = await runner._handle_codex_route_command(event)

    assert secret not in result
    assert ("applied" if returncode == 0 else "failed") in result


@pytest.mark.asyncio
async def test_codex_route_handler_force_redacts_launch_exception():
    from agent import redact

    runner = _runner()
    event = MagicMock()
    event.get_command_args.return_value = "company"
    secret = "sk" + "-proj-" + ("Y" * 40)
    failure = OSError(("Author" + "ization: Bearer ") + secret)

    with patch.object(redact, "_REDACT_ENABLED", False), patch(
        "subprocess.run", side_effect=failure
    ):
        result = await runner._handle_codex_route_command(event)

    assert secret not in result
    assert "Codex 라우팅 변경 실패" in result
