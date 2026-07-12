"""Tests for Telegram /session_fast controls."""

from types import SimpleNamespace

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli.commands import COMMAND_REGISTRY


class _PickerAdapter:
    def __init__(self):
        self.kwargs = None

    async def send_session_runtime_picker(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(success=True)


def _event() -> MessageEvent:
    return MessageEvent(
        text="/session_fast",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="user-1",
        ),
    )


def _runner(adapter):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._session_service_tier_overrides = {}
    runner._session_model_overrides = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = __import__("threading").Lock()
    return runner


def test_session_fast_command_is_registered():
    command = next(item for item in COMMAND_REGISTRY if item.name == "session-fast")
    assert command.aliases == ("session_fast",)
    assert command.gateway_only is True


@pytest.mark.asyncio
async def test_session_fast_picker_changes_only_current_session(monkeypatch):
    adapter = _PickerAdapter()
    runner = _runner(adapter)
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: {})

    result = await runner._handle_session_fast_command(_event())

    assert result is None
    assert adapter.kwargs["mode"] == "fast"
    assert adapter.kwargs["current_service_tier"] is None
    callback = adapter.kwargs["on_selected"]
    response = await callback("", "fast")
    session_key = runner._session_key_for_source(_event().source)
    assert runner._session_service_tier_overrides[session_key] == "priority"
    assert "FAST" in response


@pytest.mark.asyncio
async def test_session_fast_picker_restores_profile_default(monkeypatch):
    adapter = _PickerAdapter()
    runner = _runner(adapter)
    runner._session_service_tier_overrides[
        runner._session_key_for_source(_event().source)
    ] = "priority"
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: {})

    await runner._handle_session_fast_command(_event())
    response = await adapter.kwargs["on_selected"]("", "reset")

    assert runner._session_service_tier_overrides == {}
    assert "기본값" in response
