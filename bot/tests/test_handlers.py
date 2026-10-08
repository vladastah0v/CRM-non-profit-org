from aiogram.types import InlineKeyboardMarkup

from bot import handlers


def test_inline_keyboard_conversion():
    assert handlers.inline(None) is None
    markup = handlers.inline([[("Приду", "a:confirm:x"), ("Не смогу", "a:decline:x")]])
    assert isinstance(markup, InlineKeyboardMarkup)
    assert [b.callback_data for b in markup.inline_keyboard[0]] == ["a:confirm:x", "a:decline:x"]


def test_router_has_handlers():
    assert handlers.router.message.handlers and handlers.router.callback_query.handlers
