"""Session-only Telegram model/effort picker regression tests."""

import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class _RuntimePickerAdapter:
    """Captures the callback the session-model command gives Telegram."""

    def __init__(self):
        self.callback = None
        self.kwargs = None

    async def send_session_runtime_picker(self, *, on_selected, **kwargs):
        self.callback = on_selected
        self.kwargs = kwargs
        return types.SimpleNamespace(success=True)


def _event(text: str = "/session_model") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="owner-1",
        ),
    )


def _runner(adapter):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._running_agents = {}
    return runner


@pytest.mark.asyncio
async def test_session_model_picker_applies_model_and_effort_session_only(monkeypatch):
    """A preset tap delegates to existing handlers with session-only arguments."""
    adapter = _RuntimePickerAdapter()
    runner = _runner(adapter)
    event = _event()
    runner._handle_model_command = AsyncMock(return_value="model switched")
    runner._handle_reasoning_command = AsyncMock(return_value="effort switched")

    sent = await runner._handle_session_model_command(event)

    assert sent is None
    assert adapter.callback is not None
    assert adapter.kwargs["current_model"] == "gpt-5.6-terra"
    assert adapter.kwargs["current_effort"] == "medium"

    confirmation = await adapter.callback("gpt-5.6-sol", "high")

    model_event = runner._handle_model_command.await_args.args[0]
    effort_event = runner._handle_reasoning_command.await_args.args[0]
    assert model_event.get_command_args() == "gpt-5.6-sol --session"
    assert effort_event.get_command_args() == "high"
    assert confirmation == "model switched\n\neffort switched"


@pytest.mark.asyncio
async def test_session_model_picker_binds_durable_session_and_run_generation():
    adapter = _RuntimePickerAdapter()
    runner = _runner(adapter)
    event = _event()
    session_key = runner._session_key_for_source(event.source)
    entry = types.SimpleNamespace(session_id="session-1")
    runner.session_store = types.SimpleNamespace(
        _entries={session_key: entry},
        get_or_create_session=MagicMock(return_value=entry),
    )
    runner._session_run_generation = {session_key: 7}

    await runner._handle_session_model_command(event)

    assert adapter.kwargs["owner_user_id"] == "owner-1"
    assert adapter.kwargs["session_id"] == "session-1"
    assert adapter.kwargs["session_generation"] == 7
    is_current = adapter.kwargs["is_session_current"]
    assert is_current() is True

    runner._session_run_generation[session_key] = 8
    assert is_current() is False
    runner._session_run_generation[session_key] = 7
    runner.session_store._entries[session_key] = types.SimpleNamespace(
        session_id="session-2"
    )
    assert is_current() is False


@pytest.mark.asyncio
async def test_session_model_picker_opens_full_model_picker_session_only():
    """Detailed model selection reuses /model without allowing a global write."""
    adapter = _RuntimePickerAdapter()
    runner = _runner(adapter)
    runner._handle_model_command = AsyncMock(return_value=None)
    runner._handle_reasoning_command = AsyncMock()

    await runner._handle_session_model_command(_event())
    confirmation = await adapter.callback("", "model")

    model_event = runner._handle_model_command.await_args.args[0]
    assert model_event.get_command_args() == "--session"
    assert model_event.source == _event().source
    runner._handle_reasoning_command.assert_not_awaited()
    assert "상세 모델" in confirmation


@pytest.mark.asyncio
async def test_session_model_picker_changes_reasoning_without_changing_model():
    """Detailed effort selection is independent from the selected model."""
    adapter = _RuntimePickerAdapter()
    runner = _runner(adapter)
    runner._handle_model_command = AsyncMock()
    runner._handle_reasoning_command = AsyncMock(return_value="minimal switched")

    await runner._handle_session_model_command(_event())
    confirmation = await adapter.callback("", "minimal")

    runner._handle_model_command.assert_not_awaited()
    effort_event = runner._handle_reasoning_command.await_args.args[0]
    assert effort_event.get_command_args() == "minimal"
    assert "minimal" in confirmation


@pytest.mark.asyncio
async def test_session_model_picker_displays_disabled_reasoning_as_none():
    adapter = _RuntimePickerAdapter()
    runner = _runner(adapter)
    event = _event()
    session_key = runner._session_key_for_source(event.source)
    runner._session_reasoning_overrides[session_key] = {"enabled": False}

    await runner._handle_session_model_command(event)

    assert adapter.kwargs["current_effort"] == "none"


@pytest.mark.asyncio
async def test_session_model_typed_model_args_are_forced_session_only():
    """Typed /model-style arguments stay session-scoped too."""
    adapter = _RuntimePickerAdapter()
    runner = _runner(adapter)
    runner._handle_model_command = AsyncMock(return_value="switched")

    result = await runner._handle_session_model_command(
        _event("/session_model gpt-5.6-sol --provider openai-codex")
    )

    model_event = runner._handle_model_command.await_args.args[0]
    assert model_event.get_command_args() == (
        "gpt-5.6-sol --provider openai-codex --session"
    )
    assert result == "switched"


@pytest.mark.asyncio
async def test_session_model_picker_reset_clears_only_session_overrides(monkeypatch):
    """Reset must clear local model/reasoning overrides without a global write."""
    adapter = _RuntimePickerAdapter()
    runner = _runner(adapter)
    event = _event()
    session_key = runner._session_key_for_source(event.source)
    runner._session_model_overrides[session_key] = {"model": "gpt-5.6-sol"}
    runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}
    runner._evict_cached_agent = MagicMock()
    runner.session_store = types.SimpleNamespace(set_model_override=MagicMock())

    await runner._handle_session_model_command(event)
    confirmation = await adapter.callback("", "reset")

    assert runner._session_model_overrides == {}
    assert runner._session_reasoning_overrides == {}
    assert "기본값" in confirmation
