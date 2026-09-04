from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as module
from plugins.platforms.telegram.adapter import TelegramAdapter, _extract_copy_buttons


class CopyText:
    def __init__(self, text): self.text = text


class Button:
    def __init__(self, text, *, copy_text): self.text, self.copy_text = text, copy_text


class Markup:
    def __init__(self, inline_keyboard): self.inline_keyboard = inline_keyboard


@pytest.fixture(autouse=True)
def copy_capability(monkeypatch):
    monkeypatch.setattr(module, "CopyTextButton", CopyText)
    monkeypatch.setattr(module, "InlineKeyboardButton", Button)
    monkeypatch.setattr(module, "InlineKeyboardMarkup", Markup)


def test_extract_copy_buttons_keeps_malformed_markers_visible():
    visible, buttons = _extract_copy_buttons(
        "before\nCOPY_BUTTON: Copy|value|with|pipes\n COPY_BUTTON: indented|visible\nCOPY_BUTTON: broken")
    assert visible == "before\n COPY_BUTTON: indented|visible\nCOPY_BUTTON: broken"
    assert buttons == [("Copy", "value|with|pipes")]


@pytest.mark.asyncio
async def test_send_attaches_copy_buttons_to_last_chunk():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    adapter._bot.send_chat_action = AsyncMock(return_value=True)

    result = await adapter.send("1", "hello\nCOPY_BUTTON: Copy|secret")

    assert result.success
    call = adapter._bot.send_message.await_args
    assert "COPY_BUTTON" not in call.kwargs["text"]
    button = call.kwargs["reply_markup"].inline_keyboard[0][0]
    assert (button.text, button.copy_text.text) == ("Copy", "secret")