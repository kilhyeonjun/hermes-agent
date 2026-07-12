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
    assert argv[-2].endswith("codex_route_control.py")
    assert argv[-1] == "personal"
    assert run.call_args.kwargs["shell"] is False


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
