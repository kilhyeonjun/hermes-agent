"""Tests for Telegram model picker thread fallback."""

import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import PlatformConfig
from gateway.platforms.base import PickerCallbackOutcome
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


class TestTelegramModelPicker:
    @pytest.mark.asyncio
    async def test_picker_state_isolated_by_chat_and_message(self):
        adapter = _make_adapter()
        message_ids = iter((101, 102, 101))

        async def mock_send_message(**kwargs):
            return SimpleNamespace(message_id=next(message_ids))

        adapter._bot.send_message = AsyncMock(side_effect=mock_send_message)
        providers = [{"slug": "openai", "name": "OpenAI", "total_models": 1}]

        await adapter.send_model_picker(
            chat_id="123", providers=providers, current_model="a",
            current_provider="openai", session_key="topic-a",
            on_model_selected=AsyncMock(), metadata=None,
        )
        await adapter.send_model_picker(
            chat_id="123", providers=providers, current_model="b",
            current_provider="openai", session_key="topic-b",
            on_model_selected=AsyncMock(), metadata=None,
        )
        await adapter.send_model_picker(
            chat_id="456", providers=providers, current_model="c",
            current_provider="openai", session_key="chat-c",
            on_model_selected=AsyncMock(), metadata=None,
        )

        assert set(adapter._model_picker_state) == {
            ("123", 101), ("123", 102), ("456", 101)
        }

    @pytest.mark.asyncio
    async def test_send_model_picker_escapes_dynamic_provider_label(self):
        adapter = _make_adapter()
        sent = {}

        async def mock_send_message(**kwargs):
            sent.update(kwargs)
            return SimpleNamespace(message_id=101)

        adapter._bot.send_message = AsyncMock(side_effect=mock_send_message)

        result = await adapter.send_model_picker(
            chat_id="12345",
            providers=[
                {"slug": "provider_one", "name": "Provider One", "total_models": 1, "is_current": True}
            ],
            current_model="model_1",
            current_provider="provider_one",
            session_key="s",
            on_model_selected=AsyncMock(),
            metadata={"thread_id": "99999"},
        )

        assert result.success is True
        import plugins.platforms.telegram.adapter as telegram_mod

        assert sent["parse_mode"] == telegram_mod.ParseMode.MARKDOWN_V2
        assert "provider\\_one" in sent["text"]
        assert "`model_1`" in sent["text"]

    @pytest.mark.asyncio
    async def test_back_button_escapes_dynamic_provider_label(self):
        adapter = _make_adapter()
        adapter._model_picker_state[("12345", 42)] = {
            "providers": [{"slug": "provider_one", "name": "Provider One", "total_models": 1, "is_current": True}],
            "current_model": "model_1",
            "current_provider": "provider_one",
            "session_key": "s",
            "on_model_selected": AsyncMock(),
            "msg_id": 42,
        }

        query = AsyncMock()
        query.data = "mb"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.message_id = 42
        query.from_user = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        await adapter._handle_model_picker_callback(query, "mb", "12345")

        edit_kwargs = query.edit_message_text.call_args[1]
        import plugins.platforms.telegram.adapter as telegram_mod

        assert edit_kwargs["parse_mode"] == telegram_mod.ParseMode.MARKDOWN_V2
        assert "provider\\_one" in edit_kwargs["text"]
        assert "`model_1`" in edit_kwargs["text"]

    @pytest.mark.asyncio
    async def test_model_selected_edits_message_on_success(self):
        """Regression: the mm: (model selected → switch) success path must
        edit the picker message to show the confirmation and remove the
        buttons.  An earlier revision of this PR over-indented the
        edit_message_text block so it lived inside the except branch and
        only fired when the callback raised."""
        adapter = _make_adapter()
        callback = AsyncMock(return_value="Switched to `gpt-5`")
        adapter._model_picker_state[("12345", 42)] = {
            "providers": [
                {"slug": "openai", "name": "OpenAI", "total_models": 1, "is_current": True}
            ],
            "current_model": "model_1",
            "current_provider": "openai",
            "session_key": "s",
            "on_model_selected": callback,
            "selected_provider": "openai",
            "model_list": ["gpt-5"],
            "msg_id": 42,
            "owner_user_id": "owner-1",
            "session_id": "session-1",
            "session_generation": 1,
            "created_at": time.monotonic(),
            "is_session_current": MagicMock(return_value=True),
        }

        query = AsyncMock()
        query.data = "mm:0"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.message_id = 42
        query.from_user.id = "owner-1"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        await adapter._handle_model_picker_callback(query, "mm:0", "12345")

        callback.assert_awaited_once()
        query.edit_message_text.assert_awaited()
        edit_kwargs = query.edit_message_text.call_args[1]
        import plugins.platforms.telegram.adapter as telegram_mod

        assert edit_kwargs["parse_mode"] == telegram_mod.ParseMode.MARKDOWN_V2
        assert "`gpt-5`" in edit_kwargs["text"]
        query.answer.assert_awaited_with(text="Model switched!")
        assert ("12345", 42) not in adapter._model_picker_state

    @pytest.mark.asyncio
    @pytest.mark.parametrize("callback_data", ["mm:0", "mc:0"])
    @pytest.mark.parametrize(
        ("status", "expected_toast"),
        [
            ("expired", "Picker expired."),
            ("cancelled", "Switch cancelled."),
            ("failure", "Switch failed."),
        ],
    )
    async def test_non_success_callback_outcome_never_emits_model_switched_toast(
        self, monkeypatch, callback_data, status, expected_toast
    ):
        adapter = _make_adapter()
        callback = AsyncMock(
            return_value=PickerCallbackOutcome(
                f"callback ended with {status}", status=status
            )
        )
        adapter._model_picker_state[("12345", 42)] = {
            "providers": [{"slug": "openai", "name": "OpenAI"}],
            "current_model": "model_1",
            "current_provider": "openai",
            "session_key": "s",
            "on_model_selected": callback,
            "selected_provider": "openai",
            "model_list": ["gpt-5"],
            "msg_id": 42,
            "owner_user_id": "owner-1",
            "session_id": "session-1",
            "session_generation": 1,
            "created_at": time.monotonic(),
            "is_session_current": MagicMock(return_value=True),
        }
        monkeypatch.setattr(
            "hermes_cli.model_cost_guard.expensive_model_warning",
            lambda *_args, **_kwargs: None,
        )
        query = SimpleNamespace(
            message=SimpleNamespace(message_id=42),
            from_user=SimpleNamespace(id="owner-1"),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        await adapter._handle_model_picker_callback(
            query, callback_data, "12345"
        )

        callback.assert_awaited_once()
        query.answer.assert_awaited_with(text=expected_toast)
        assert query.answer.await_args.kwargs["text"] != "Model switched!"

    @pytest.mark.asyncio
    async def test_provider_group_folds_and_drills_down(self, monkeypatch):
        """A provider family (e.g. MiniMax) collapses to one mpg: button at
        the top level; tapping it expands to its authenticated members as
        mp: buttons. A group reduced to a single authenticated member shows
        no submenu (direct mp: button).

        Inspects callback_data by recording every InlineKeyboardButton built,
        which is robust to whether `telegram` is the real SDK or the module
        mock (the SDK markup objects don't expose a plain iterable under the
        mock)."""
        import plugins.platforms.telegram.adapter as tg

        built: list = []

        class _RecordingButton:
            def __init__(self, text, callback_data=None, **kw):
                self.text = text
                self.callback_data = callback_data
                built.append(callback_data)

        class _RecordingMarkup:
            def __init__(self, rows):
                self.inline_keyboard = rows

        monkeypatch.setattr(tg, "InlineKeyboardButton", _RecordingButton)
        monkeypatch.setattr(tg, "InlineKeyboardMarkup", _RecordingMarkup)

        adapter = _make_adapter()

        async def mock_send_message(**kwargs):
            return SimpleNamespace(message_id=101)

        adapter._bot.send_message = AsyncMock(side_effect=mock_send_message)

        providers = [
            {"slug": "minimax", "name": "MiniMax", "total_models": 2},
            {"slug": "minimax-cn", "name": "MiniMax (China)", "total_models": 3},
            {"slug": "xai", "name": "xAI", "total_models": 1},
        ]

        await adapter.send_model_picker(
            chat_id="12345",
            providers=providers,
            current_model="m",
            current_provider="minimax",
            session_key="s",
            on_model_selected=AsyncMock(),
            metadata=None,
        )

        assert "mpg:minimax" in built
        assert "mp:xai" in built
        assert "mp:minimax" not in built
        assert "mp:minimax-cn" not in built

        built.clear()
        query = AsyncMock()
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.message_id = 101
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        await adapter._handle_model_picker_callback(query, "mpg:minimax", "12345")

        assert "mp:minimax" in built
        assert "mp:minimax-cn" in built
        assert "mb" in built

    @pytest.mark.asyncio
    async def test_provider_picker_paginates_past_first_ten(self, monkeypatch):
        import plugins.platforms.telegram.adapter as tg

        class _RecordingButton:
            def __init__(self, text, callback_data=None, **kw):
                self.text = text
                self.callback_data = callback_data

        class _RecordingMarkup:
            def __init__(self, rows):
                self.inline_keyboard = rows

        monkeypatch.setattr(tg, "InlineKeyboardButton", _RecordingButton)
        monkeypatch.setattr(tg, "InlineKeyboardMarkup", _RecordingMarkup)

        adapter = _make_adapter()
        sent = {}

        async def mock_send_message(**kwargs):
            sent.update(kwargs)
            return SimpleNamespace(message_id=101)

        adapter._bot.send_message = AsyncMock(side_effect=mock_send_message)

        providers = [
            {"slug": f"provider-{i}", "name": f"Provider {i}", "total_models": 1}
            for i in range(10)
        ]
        providers.append({
            "slug": "zai",
            "name": "Z.AI / GLM",
            "models": ["glm-5.2"],
            "total_models": 1,
        })

        await adapter.send_model_picker(
            chat_id="12345",
            providers=providers,
            current_model="model_1",
            current_provider="provider-0",
            session_key="s",
            on_model_selected=AsyncMock(),
            metadata=None,
        )

        def _callbacks(markup):
            return [
                button.callback_data
                for row in markup.inline_keyboard
                for button in row
            ]

        first_page = _callbacks(sent["reply_markup"])
        assert "mp:zai" not in first_page
        assert "mpv:1" in first_page

        query = AsyncMock()
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.message_id = 101
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        await adapter._handle_model_picker_callback(query, "mpv:1", "12345")

        second_page = _callbacks(query.edit_message_text.call_args[1]["reply_markup"])
        assert "mp:zai" in second_page
        assert "mpv:0" in second_page

        await adapter._handle_model_picker_callback(query, "mp:zai", "12345")
        assert adapter._model_picker_state[("12345", 101)]["selected_provider"] == "zai"

        await adapter._handle_model_picker_callback(query, "mb", "12345")
        back_page = _callbacks(query.edit_message_text.call_args[1]["reply_markup"])
        assert "mp:zai" in back_page

    @pytest.mark.asyncio
    async def test_expensive_model_requires_confirmation(self, monkeypatch):
        adapter = _make_adapter()
        callback = AsyncMock(return_value="Switched to `openai/gpt-5.5-pro`")
        adapter._model_picker_state[("12345", 42)] = {
            "providers": [
                {"slug": "openrouter", "name": "OpenRouter", "total_models": 1, "is_current": True}
            ],
            "current_model": "model_1",
            "current_provider": "openrouter",
            "session_key": "s",
            "on_model_selected": callback,
            "selected_provider": "openrouter",
            "model_list": ["openai/gpt-5.5-pro"],
            "msg_id": 42,
            "owner_user_id": "owner-1",
            "session_id": "session-1",
            "session_generation": 1,
            "created_at": time.monotonic(),
            "is_session_current": MagicMock(return_value=True),
        }
        monkeypatch.setattr(
            "hermes_cli.model_cost_guard.expensive_model_warning",
            lambda *_args, **_kwargs: SimpleNamespace(
                message="!!! EXPENSIVE MODEL WARNING !!!\ndid you mean to select openai/gpt-5.5?"
            ),
        )

        query = AsyncMock()
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.message_id = 42
        query.from_user.id = "owner-1"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        await adapter._handle_model_picker_callback(query, "mm:0", "12345")

        callback.assert_not_awaited()
        assert ("12345", 42) in adapter._model_picker_state
        first_edit = query.edit_message_text.call_args[1]
        assert "EXPENSIVE MODEL WARNING" in first_edit["text"]
        assert first_edit["reply_markup"] is not None

        await adapter._handle_model_picker_callback(query, "mc:0", "12345")

        callback.assert_awaited_once_with("12345", "openai/gpt-5.5-pro", "openrouter")
        assert ("12345", 42) not in adapter._model_picker_state

    @pytest.mark.asyncio
    async def test_session_is_revalidated_after_cost_lookup_yields(self, monkeypatch):
        import plugins.platforms.telegram.adapter as telegram_mod

        adapter = _make_adapter()
        callback = AsyncMock(return_value="changed")
        adapter._model_picker_state[("12345", 42)] = {
            "providers": [{"slug": "openrouter", "name": "OpenRouter"}],
            "current_model": "old",
            "current_provider": "openrouter",
            "session_key": "telegram:12345",
            "on_model_selected": callback,
            "selected_provider": "openrouter",
            "model_list": ["safe-model"],
            "owner_user_id": "owner-1",
            "session_id": "session-old",
            "session_generation": 1,
            "created_at": time.monotonic(),
            "is_session_current": MagicMock(return_value=True),
        }

        async def reset_during_cost_lookup(_func, *_args, **_kwargs):
            adapter._model_picker_state.clear()
            return None

        monkeypatch.setattr(telegram_mod.asyncio, "to_thread", reset_during_cost_lookup)
        query = SimpleNamespace(
            message=SimpleNamespace(message_id=42),
            from_user=SimpleNamespace(id="owner-1"),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        await adapter._handle_model_picker_callback(query, "mm:0", "12345")

        callback.assert_not_awaited()
        query.answer.assert_awaited_with(
            text="Picker expired — open a new picker in the current session."
        )

    @pytest.mark.asyncio
    async def test_retries_without_thread_when_thread_not_found(self):
        adapter = _make_adapter()
        providers = [{"slug": "openai", "name": "OpenAI", "total_models": 2, "is_current": True}]
        call_log = []

        class FakeBadRequest(Exception):
            pass

        async def mock_send_message(**kwargs):
            call_log.append(dict(kwargs))
            if kwargs.get("message_thread_id") is not None:
                raise FakeBadRequest("Message thread not found")
            return SimpleNamespace(message_id=99)

        adapter._bot.send_message = AsyncMock(side_effect=mock_send_message)

        result = await adapter.send_model_picker(
            chat_id="12345",
            providers=providers,
            current_model="gpt-5",
            current_provider="openai",
            session_key="s",
            on_model_selected=AsyncMock(),
            metadata={"thread_id": "99999"},
        )

        assert result.success is True
        assert len(call_log) == 2
        assert call_log[0]["message_thread_id"] == 99999
        assert "message_thread_id" not in call_log[1] or call_log[1]["message_thread_id"] is None

    @pytest.mark.asyncio
    async def test_send_model_picker_binds_owner_session_and_creation_time(self):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(
            return_value=SimpleNamespace(message_id=42)
        )
        is_session_current = MagicMock(return_value=True)

        result = await adapter.send_model_picker(
            chat_id="12345",
            providers=[{"slug": "openai", "name": "OpenAI", "total_models": 1}],
            current_model="gpt-5",
            current_provider="openai",
            session_key="telegram:12345",
            on_model_selected=AsyncMock(),
            owner_user_id="owner-1",
            session_id="session-1",
            session_generation=7,
            is_session_current=is_session_current,
        )

        assert result.success is True
        state = adapter._model_picker_state[("12345", 42)]
        assert state["owner_user_id"] == "owner-1"
        assert state["session_id"] == "session-1"
        assert state["session_generation"] == 7
        assert state["is_session_current"] is is_session_current
        assert isinstance(state["created_at"], float)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("callback_data", "state_attr"),
        [
            ("sr:sol", "_session_runtime_picker_state"),
            ("mm:0", "_model_picker_state"),
        ],
    )
    async def test_authorized_non_owner_cannot_use_same_chat_picker(
        self, callback_data, state_attr
    ):
        adapter = _make_adapter()
        adapter._is_callback_user_authorized = MagicMock(return_value=True)
        callback = AsyncMock(return_value="changed")
        state = {
            "owner_user_id": "owner-1",
            "session_key": "telegram:12345",
            "session_id": "session-1",
            "session_generation": 7,
            "created_at": time.monotonic(),
            "is_session_current": MagicMock(return_value=True),
            "on_selected": callback,
            "on_model_selected": callback,
            "model_list": ["gpt-5"],
            "selected_provider": "openai",
        }
        getattr(adapter, state_attr)[("12345", 42)] = state
        query = SimpleNamespace(
            data=callback_data,
            message=SimpleNamespace(
                chat_id=12345,
                message_id=42,
                chat=SimpleNamespace(type="supergroup"),
                message_thread_id=5,
            ),
            from_user=SimpleNamespace(id="other-authorized", first_name="Other"),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), SimpleNamespace()
        )

        callback.assert_not_awaited()
        query.answer.assert_awaited_once_with(
            text="⛔ Only the user who opened this picker can use it."
        )
        assert ("12345", 42) in getattr(adapter, state_attr)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stale_reason", ["session", "ttl"])
    async def test_stale_picker_is_rejected_and_purged(self, stale_reason):
        adapter = _make_adapter()
        adapter._is_callback_user_authorized = MagicMock(return_value=True)
        callback = AsyncMock(return_value="changed")
        adapter._session_runtime_picker_state[("12345", 42)] = {
            "owner_user_id": "owner-1",
            "session_key": "telegram:12345",
            "session_id": "session-old",
            "session_generation": 7,
            "created_at": (
                time.monotonic() - adapter._PICKER_TTL_SECONDS - 1
                if stale_reason == "ttl"
                else time.monotonic()
            ),
            "is_session_current": MagicMock(
                return_value=stale_reason != "session"
            ),
            "on_selected": callback,
        }
        query = SimpleNamespace(
            data="sr:sol",
            message=SimpleNamespace(
                chat_id=12345,
                message_id=42,
                chat=SimpleNamespace(type="private"),
                message_thread_id=None,
            ),
            from_user=SimpleNamespace(id="owner-1", first_name="Owner"),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), SimpleNamespace()
        )

        callback.assert_not_awaited()
        query.answer.assert_awaited_once_with(
            text="Picker expired — open a new picker in the current session."
        )
        assert adapter._session_runtime_picker_state == {}
