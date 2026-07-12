"""Regression tests for gateway inline-keyboard model-picker persistence.

#49066 made the typed ``/model <name>`` command persist the selected model to
``config.yaml`` by default. But the inline-keyboard picker callback
(``_on_model_selected`` in ``gateway/slash_commands.py``) was left session-only:
it hard-coded ``is_global=False`` and never wrote ``config.yaml``, so *tapping* a
model in the Telegram/Discord picker silently reverted on the next launch while
*typing* the same model persisted — a contradiction the same PR introduced.

After the fix (#49176), the picker callback honors the resolved
``persist_global`` (defaults to ``True``, still respects ``--session``) and runs
the same read-modify-write block the text path uses, so a tapped model survives
across sessions like a typed one.

These tests drive the real ``_handle_model_command`` with a fake picker-capable
adapter that captures the ``on_model_selected`` callback, then invoke that
callback and assert ``config.yaml`` is (or isn't) updated — exercising the exact
closure the PR changed, against a real temp ``HERMES_HOME``.
"""

import asyncio
import dataclasses
import threading
import typing
import types
from unittest.mock import AsyncMock, MagicMock

import yaml
import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, PickerCallbackOutcome
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def test_picker_session_binding_type_hints_resolve():
    from gateway.slash_commands import GatewaySlashCommandsMixin

    hints = typing.get_type_hints(GatewaySlashCommandsMixin._picker_session_binding)

    assert hints["return"] == dict[str, typing.Any]


class _FakePickerAdapter:
    """Minimal adapter that looks picker-capable and captures the callback.

    ``_handle_model_command`` gates the picker path on
    ``getattr(type(adapter), "send_model_picker", None) is not None``, so the
    method must exist on the class, not just the instance.
    """

    def __init__(self):
        self.captured_callback = None
        self.captured_kwargs = None

    async def send_model_picker(self, *, on_model_selected, **kwargs):
        # Stash the closure the handler built so the test can fire a "tap".
        self.captured_callback = on_model_selected
        self.captured_kwargs = kwargs
        return types.SimpleNamespace(success=True)


def _make_runner(adapter, platform=Platform.TELEGRAM):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {platform: adapter}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._running_agents = {}
    source = _make_event("/model", platform).source
    session_key = runner._session_key_for_source(source)
    entry = types.SimpleNamespace(session_id="session-1")
    runner.session_store = types.SimpleNamespace(
        _entries={session_key: entry},
        get_or_create_session=MagicMock(return_value=entry),
        set_model_override=MagicMock(),
    )
    runner._session_run_generation = {session_key: 0}
    return runner


def _make_event(text, platform=Platform.TELEGRAM):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=platform,
            chat_id="12345",
            chat_type="dm",
            user_id="owner-1",
        ),
    )


def _fake_switch_result():
    """A successful ModelSwitchResult that bypasses real provider resolution."""
    from hermes_cli.model_switch import ModelSwitchResult

    return ModelSwitchResult(
        success=True,
        new_model="gpt-5.5",
        target_provider="openrouter",
        provider_changed=True,
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions",
        provider_label="OpenRouter",
        is_global=True,
    )


def _setup_isolated_home(tmp_path, monkeypatch, model_yaml_value):
    """Write a config.yaml with the given ``model:`` value and stub heavy bits."""
    import gateway.run as gateway_run

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({"model": model_yaml_value, "providers": {}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    # The picker-setup path calls list_picker_providers, which otherwise hits
    # the network (OpenRouter model catalog). Stub it to a minimal list — these
    # tests capture and fire the on_model_selected callback and don't assert on
    # picker contents. The handler imports it as a local alias at call time, so
    # patching the source-module attribute takes effect.
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_picker_providers",
        lambda **kw: [{"slug": "openrouter", "name": "OpenRouter", "models": ["gpt-5.5"]}],
    )
    # switch_model is imported as a local alias inside the handler
    # (`from hermes_cli.model_switch import switch_model as _switch_model`),
    # so patching the source-module attribute takes effect at call time.
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kw: _fake_switch_result(),
    )
    # The confirmation builder resolves context length for display, which
    # otherwise makes real outbound HTTP calls (Ollama /api/show + the
    # OpenRouter models catalog). Stub it — these tests don't assert on the
    # displayed context, and the closure imports it lazily from this module.
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length",
        lambda *a, **k: 272000,
    )
    # save_config writes to ``get_hermes_home() / config.yaml`` — point it here.
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: hermes_home)
    return cfg_path


async def _drive_picker(runner, event):
    """Run the handler (which sends the picker) then fire the captured tap."""
    sent = await runner._handle_model_command(event)
    # Bare /model returns None (picker sent); the adapter captured the callback.
    assert sent is None
    adapter = runner.adapters[Platform.TELEGRAM]
    assert adapter.captured_callback is not None, "picker callback was not wired"
    # Simulate the user tapping "gpt-5.5" under the openrouter provider.
    return await adapter.captured_callback("12345", "gpt-5.5", "openrouter")


@pytest.mark.asyncio
async def test_generic_model_picker_receives_owner_and_session_binding(
    tmp_path, monkeypatch
):
    adapter = _FakePickerAdapter()
    _setup_isolated_home(
        tmp_path,
        monkeypatch,
        {"default": "old-model", "provider": "openai-codex"},
    )
    runner = _make_runner(adapter)
    event = _make_event("/model")
    session_key = runner._session_key_for_source(event.source)
    entry = types.SimpleNamespace(session_id="session-1")
    runner.session_store = types.SimpleNamespace(
        _entries={session_key: entry},
        get_or_create_session=MagicMock(return_value=entry),
    )
    runner._session_run_generation = {session_key: 3}

    assert await runner._handle_model_command(event) is None

    assert adapter.captured_kwargs["owner_user_id"] == "owner-1"
    assert adapter.captured_kwargs["session_id"] == "session-1"
    assert adapter.captured_kwargs["session_generation"] == 3
    assert adapter.captured_kwargs["is_session_current"]() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.DISCORD, Platform.MATRIX])
async def test_non_telegram_picker_does_not_receive_telegram_binding_kwargs(
    tmp_path, monkeypatch, platform
):
    adapter = _FakePickerAdapter()
    _setup_isolated_home(
        tmp_path,
        monkeypatch,
        {"default": "old-model", "provider": "openai-codex"},
    )
    runner = _make_runner(adapter, platform)

    assert await runner._handle_model_command(_make_event("/model", platform)) is None

    assert "owner_user_id" not in adapter.captured_kwargs
    assert "session_id" not in adapter.captured_kwargs
    assert "session_generation" not in adapter.captured_kwargs
    assert "is_session_current" not in adapter.captured_kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seed_model",
    [
        # Already-nested dict (common case).
        {
            "default": "old-model",
            "provider": "custom",
            "base_url": "https://api.custom.example/v1",
            "api_key": "sk-stale",
            "api_mode": "anthropic_messages",
        },
        # Flat-string model: must be coerced to a nested dict on a tap (same
        # scalar-``model:`` guard the text path has) instead of raising
        # ``TypeError`` on assignment.
        "deepseek-v4-flash",
    ],
    ids=["nested-dict", "flat-string"],
)
async def test_picker_tap_persists_by_default(tmp_path, monkeypatch, seed_model):
    """Tapping a model in the picker (bare /model) persists to config.yaml,
    matching the typed ``/model`` default — this is the #49176 fix. The written
    ``model:`` must always end up a nested dict regardless of the seed shape."""
    adapter = _FakePickerAdapter()
    cfg_path = _setup_isolated_home(tmp_path, monkeypatch, seed_model)

    confirmation = await _drive_picker(_make_runner(adapter), _make_event("/model"))

    assert confirmation is not None
    assert "gpt-5.5" in confirmation
    written = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert isinstance(written["model"], dict), (
        "model: should be coerced to a dict, got %r" % (written["model"],)
    )
    assert written["model"]["default"] == "gpt-5.5"
    assert written["model"]["provider"] == "openrouter"
    assert "base_url" not in written["model"]
    assert "api_key" not in written["model"]
    assert "api_mode" not in written["model"]


@pytest.mark.asyncio
async def test_picker_tap_session_flag_does_not_persist(tmp_path, monkeypatch):
    """``/model --session`` then a picker tap stays in-memory only — config
    untouched, but the in-memory session override must still be applied (the
    switch worked, it just wasn't persisted)."""
    adapter = _FakePickerAdapter()
    cfg_path = _setup_isolated_home(
        tmp_path, monkeypatch, {"default": "old-model", "provider": "openai-codex"}
    )
    runner = _make_runner(adapter)

    confirmation = await _drive_picker(runner, _make_event("/model --session"))

    assert confirmation is not None
    assert "gpt-5.5" in confirmation
    # The session override IS applied in-memory (proves the path didn't no-op).
    assert runner._session_model_overrides, "session override should be set"
    assert any(
        ov.get("model") == "gpt-5.5"
        for ov in runner._session_model_overrides.values()
    )
    # But config.yaml is untouched — the override is in-memory only.
    written = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert written["model"]["default"] == "old-model"
    assert written["model"]["provider"] == "openai-codex"


@pytest.mark.asyncio
async def test_picker_resolves_without_global_side_effects_before_session_revalidation(
    tmp_path, monkeypatch
):
    """The worker-thread resolution phase must stay side-effect free.

    Global persistence belongs to the guarded callback commit phase, after the
    concrete Telegram session has been revalidated.
    """
    adapter = _FakePickerAdapter()
    cfg_path = _setup_isolated_home(
        tmp_path,
        monkeypatch,
        {"default": "old-model", "provider": "openai-codex"},
    )
    switch_kwargs = {}

    def _capture_switch(**kwargs):
        switch_kwargs.update(kwargs)
        return _fake_switch_result()

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", _capture_switch)

    confirmation = await _drive_picker(_make_runner(adapter), _make_event("/model"))

    assert switch_kwargs["is_global"] is False
    assert "gpt-5.5" in confirmation
    written = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert written["model"]["default"] == "gpt-5.5"


@pytest.mark.asyncio
async def test_picker_revalidates_after_blocking_switch_before_mutating_replacement_session(
    tmp_path, monkeypatch
):
    """A /new racing the worker-thread switch makes the old tap a no-op."""
    adapter = _FakePickerAdapter()
    cfg_path = _setup_isolated_home(
        tmp_path,
        monkeypatch,
        {"default": "old-model", "provider": "openai-codex"},
    )
    runner = _make_runner(adapter)
    event = _make_event("/model")
    session_key = runner._session_key_for_source(event.source)
    cached_agent = MagicMock()
    runner._agent_cache = {session_key: (cached_agent,)}
    runner._agent_cache_lock = threading.Lock()
    runner._session_db = types.SimpleNamespace(update_session_model=AsyncMock())
    switch_started = threading.Event()
    release_switch = threading.Event()

    def _blocking_switch(**_kwargs):
        switch_started.set()
        if not release_switch.wait(timeout=5):
            raise AssertionError("test did not release the blocked model switch")
        return _fake_switch_result()

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", _blocking_switch)

    assert await runner._handle_model_command(event) is None
    callback_task = asyncio.create_task(
        adapter.captured_callback("12345", "gpt-5.5", "openrouter")
    )
    try:
        assert await asyncio.wait_for(
            asyncio.to_thread(switch_started.wait, 3), timeout=4
        )
        replacement = types.SimpleNamespace(session_id="session-2")
        runner.session_store._entries[session_key] = replacement
        runner.session_store.get_or_create_session.return_value = replacement
        runner._session_run_generation[session_key] += 1
    finally:
        release_switch.set()

    confirmation = await asyncio.wait_for(callback_task, timeout=5)

    assert isinstance(confirmation, PickerCallbackOutcome)
    assert confirmation.status == "expired"
    assert "expired" in confirmation.lower()
    assert runner._session_model_overrides == {}
    assert getattr(runner, "_pending_model_notes", {}) == {}
    cached_agent.switch_model.assert_not_called()
    runner.session_store.set_model_override.assert_not_called()
    runner._session_db.update_session_model.assert_not_called()
    written = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert written["model"]["default"] == "old-model"
    assert written["model"]["provider"] == "openai-codex"


@pytest.mark.asyncio
async def test_picker_revalidates_after_db_await_before_session_and_config_commit(
    tmp_path, monkeypatch
):
    """A /new during the DB await prevents every subsequent picker commit."""
    adapter = _FakePickerAdapter()
    cfg_path = _setup_isolated_home(
        tmp_path,
        monkeypatch,
        {"default": "old-model", "provider": "openai-codex"},
    )
    runner = _make_runner(adapter)
    event = _make_event("/model")
    session_key = runner._session_key_for_source(event.source)
    db_started = asyncio.Event()
    release_db = asyncio.Event()

    async def _blocking_db_update(*_args):
        db_started.set()
        await release_db.wait()

    runner._session_db = types.SimpleNamespace(
        update_session_model=AsyncMock(side_effect=_blocking_db_update)
    )

    assert await runner._handle_model_command(event) is None
    callback_task = asyncio.create_task(
        adapter.captured_callback("12345", "gpt-5.5", "openrouter")
    )
    await asyncio.wait_for(db_started.wait(), timeout=4)
    replacement = types.SimpleNamespace(session_id="session-2")
    runner.session_store._entries[session_key] = replacement
    runner.session_store.get_or_create_session.return_value = replacement
    runner._session_run_generation[session_key] += 1
    release_db.set()

    confirmation = await asyncio.wait_for(callback_task, timeout=5)

    assert isinstance(confirmation, PickerCallbackOutcome)
    assert confirmation.status == "expired"
    assert "expired" in confirmation.lower()
    assert runner._session_model_overrides == {}
    assert getattr(runner, "_pending_model_notes", {}) == {}
    runner.session_store.set_model_override.assert_not_called()
    written = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert written["model"]["default"] == "old-model"
    assert written["model"]["provider"] == "openai-codex"


@pytest.mark.asyncio
async def test_recovered_topic_picker_commits_only_to_bound_normalized_session(
    tmp_path, monkeypatch
):
    """The callback must keep using the topic-recovered source it bound."""
    adapter = _FakePickerAdapter()
    _setup_isolated_home(
        tmp_path,
        monkeypatch,
        {"default": "old-model", "provider": "openai-codex"},
    )
    runner = _make_runner(adapter)
    event = _make_event("/model")
    recovered_source = dataclasses.replace(event.source, thread_id="topic-7")
    raw_key = runner._session_key_for_source(event.source)
    topic_key = runner._session_key_for_source(recovered_source)
    assert raw_key != topic_key

    raw_entry = types.SimpleNamespace(session_id="raw-session")
    topic_entry = types.SimpleNamespace(session_id="topic-session")

    class _TopicAwareStore:
        def __init__(self):
            self._entries = {raw_key: raw_entry, topic_key: topic_entry}
            self.set_model_override = MagicMock()

        def get_or_create_session(self, source):
            return self._entries[runner._session_key_for_source(source)]

    runner.session_store = _TopicAwareStore()
    runner._session_run_generation = {raw_key: 0, topic_key: 0}
    runner._normalize_source_for_session_key = MagicMock(
        return_value=recovered_source
    )
    stored_models = {
        raw_entry.session_id: "raw-old",
        topic_entry.session_id: "topic-old",
    }

    async def _update_session_model(session_id, model):
        stored_models[session_id] = model

    runner._session_db = types.SimpleNamespace(
        update_session_model=AsyncMock(side_effect=_update_session_model)
    )
    enrich = MagicMock()
    monkeypatch.setattr(
        "hermes_cli.context_switch_guard.enrich_model_switch_warnings_for_gateway",
        enrich,
    )

    assert await runner._handle_model_command(event) is None
    confirmation = await adapter.captured_callback(
        "12345", "gpt-5.5", "openrouter"
    )

    assert isinstance(confirmation, PickerCallbackOutcome)
    assert confirmation.status == "success"
    assert "gpt-5.5" in confirmation
    assert stored_models == {
        raw_entry.session_id: "raw-old",
        topic_entry.session_id: "gpt-5.5",
    }
    enrich.assert_called_once()
    assert enrich.call_args.kwargs["source"] == recovered_source
    runner.session_store.set_model_override.assert_called_once()
    assert runner.session_store.set_model_override.call_args.args[0] == topic_key
