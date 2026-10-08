"""Обработчики aiogram: принимают сообщения и нажатия, отправляют ответы из logic.py.

Бот работает только в личных чатах: в группе сведения о визите увидели бы посторонние.
Тексты сообщений и Telegram ID в журнал не пишутся.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from . import logic, texts
from .crm import CrmClient

router = Router(name="volunteers")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=texts.MENU_SHIFTS), KeyboardButton(text=texts.MENU_SIGNUP)],
        [KeyboardButton(text=texts.MENU_HELP)],
    ],
    resize_keyboard=True,
)


class Attendance(StatesGroup):
    waiting_hours = State()


def inline(buttons: logic.Keyboard | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in buttons
        ]
    )


async def send(message: Message, replies: list[logic.Reply]) -> None:
    for reply in replies:
        markup = MENU if reply.menu and not reply.buttons else inline(reply.buttons)
        await message.answer(reply.text, reply_markup=markup)


def _user_id(message: Message) -> int:
    if message.from_user is None:  # в личном чате отправитель есть всегда
        raise RuntimeError("Сообщение без отправителя")
    return message.from_user.id


@router.message(CommandStart())
async def on_start(
    message: Message, command: CommandObject, crm: CrmClient, state: FSMContext
) -> None:
    await state.clear()
    await send(message, await logic.start(crm, _user_id(message), command.args))


@router.message(Command("help"))
@router.message(F.text == texts.MENU_HELP)
async def on_help(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=MENU)


@router.message(Command("shifts"))
@router.message(F.text == texts.MENU_SHIFTS)
async def on_my_shifts(message: Message, crm: CrmClient, state: FSMContext) -> None:
    await state.clear()
    await send(message, await logic.my_shifts(crm, _user_id(message)))


@router.message(Command("signup"))
@router.message(F.text == texts.MENU_SIGNUP)
async def on_open_shifts(message: Message, crm: CrmClient, state: FSMContext) -> None:
    await state.clear()
    await send(message, await logic.open_shifts(crm, _user_id(message)))


@router.message(Command("unlink"))
async def on_unlink(message: Message, crm: CrmClient, state: FSMContext) -> None:
    await state.clear()
    await send(message, await logic.unlink(crm, _user_id(message)))


@router.message(Attendance.waiting_hours)
async def on_hours(message: Message, crm: CrmClient, state: FSMContext) -> None:
    data = await state.get_data()
    replies, done = await logic.submit_hours(
        crm, _user_id(message), data["assignment_id"], message.text
    )
    if done:
        await state.clear()
    await send(message, replies)


@router.message(F.text.func(texts.looks_like_code))
async def on_code(message: Message, crm: CrmClient) -> None:
    await send(message, await logic.link(crm, _user_id(message), message.text or ""))


@router.message()
async def on_other(message: Message) -> None:
    await message.answer(
        "Не понял сообщение. Выберите действие в меню или отправьте /help.", reply_markup=MENU
    )


@router.callback_query()
async def on_button(query: CallbackQuery, crm: CrmClient, state: FSMContext) -> None:
    result = await logic.on_callback(crm, query.from_user.id, query.data)
    await query.answer(result.notice or None)
    message = query.message
    if not isinstance(message, Message):
        return  # сообщение слишком старое — ответить в него нельзя
    if result.remove_buttons:
        await message.edit_reply_markup(reply_markup=None)
    if result.ask_hours_for:
        await state.set_state(Attendance.waiting_hours)
        await state.update_data(assignment_id=result.ask_hours_for)
    await send(message, result.replies)
