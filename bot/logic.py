"""Сценарии бота без привязки к aiogram: на входе — запрос волонтёра, на выходе — ответы.

Так сценарии проверяются тестами без Telegram, а handlers.py только отправляет ответы.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import callbacks, texts
from .crm import CrmClient, CrmError, CrmUnavailableError, NotLinkedError

Button = tuple[str, str]  # текст, данные кнопки
Keyboard = list[list[Button]]
MAX_ITEMS = 10


@dataclass
class Reply:
    text: str
    buttons: Keyboard | None = None
    menu: bool = False  # показать постоянное меню внизу


@dataclass
class CallbackResult:
    notice: str = ""  # короткое всплывающее сообщение
    replies: list[Reply] = field(default_factory=list)
    remove_buttons: bool = False  # ответ принят — кнопки под сообщением больше не нужны
    ask_hours_for: str | None = None  # ждать от волонтёра число часов


async def _safe(action: Callable[[], Awaitable[list[Reply]]]) -> list[Reply]:
    try:
        return await action()
    except NotLinkedError:
        return [Reply(texts.NOT_LINKED)]
    except CrmUnavailableError:
        return [Reply(texts.UNAVAILABLE)]
    except CrmError as exc:
        return [Reply(exc.detail)]


def assignment_buttons(a: dict[str, Any]) -> Keyboard:
    pk = a["id"]
    rows: Keyboard = []
    answer: list[Button] = []
    if a.get("can_confirm"):
        answer.append(("Приду", callbacks.assignment("confirm", pk)))
    if a.get("can_decline"):
        answer.append(("Не смогу", callbacks.assignment("decline", pk)))
    if answer:
        rows.append(answer)
    if a.get("needs_attendance"):
        hours = texts.format_hours(a.get("default_hours"))
        rows += [
            [(f"Был(а), {hours} ч", callbacks.assignment("attended", pk))],
            [("Другое число часов", callbacks.assignment("hours", pk))],
            [("Не смог(ла) прийти", callbacks.assignment("noshow", pk))],
        ]
    elif answer:
        label = "Подробнее о визите" if a["kind"] == "visit" else "Подробнее"
        rows.append([(label, callbacks.assignment("details", pk))])
    return rows


# ---------------------------------------------------------------------------
# Команды и меню
# ---------------------------------------------------------------------------
async def start(crm: CrmClient, telegram_id: int, code: str | None = None) -> list[Reply]:
    """/start: привязка по коду из ссылки или приветствие привязанного волонтёра."""
    if code:
        return await link(crm, telegram_id, code)

    async def run() -> list[Reply]:
        me = await crm.me(telegram_id)
        return [Reply(texts.me_text(me) + "\n\n" + texts.HELP, menu=True)]

    try:
        return await run()
    except NotLinkedError:
        return [Reply(texts.ASK_CODE)]
    except CrmError:
        return await _safe(run)


async def link(crm: CrmClient, telegram_id: int, code: str) -> list[Reply]:
    async def run() -> list[Reply]:
        data = await crm.link(telegram_id, code.strip())
        return [
            Reply(
                f"Готово, {data['first_name']}! Telegram привязан к вашей карточке волонтёра.\n\n"
                + texts.HELP,
                menu=True,
            )
        ]

    return await _safe(run)


async def unlink(crm: CrmClient, telegram_id: int) -> list[Reply]:
    async def run() -> list[Reply]:
        await crm.unlink(telegram_id)
        return [
            Reply(
                "Telegram отвязан. Напоминания приходить не будут. "
                "Чтобы подключиться снова, попросите у координатора новый код."
            )
        ]

    return await _safe(run)


async def my_shifts(crm: CrmClient, telegram_id: int) -> list[Reply]:
    async def run() -> list[Reply]:
        items = await crm.assignments(telegram_id)
        if not items:
            return [Reply(f"Предстоящих смен и визитов нет. Посмотрите «{texts.MENU_SIGNUP}».")]
        replies = [
            Reply(texts.assignment_text(a), assignment_buttons(a) or None)
            for a in items[:MAX_ITEMS]
        ]
        if len(items) > MAX_ITEMS:
            replies.append(Reply(f"Показаны ближайшие {MAX_ITEMS}."))
        return replies

    return await _safe(run)


async def open_shifts(crm: CrmClient, telegram_id: int) -> list[Reply]:
    async def run() -> list[Reply]:
        data = await crm.open_shifts(telegram_id)
        items = data.get("results", [])
        if not items:
            return [Reply(data.get("detail") or "Открытых смен для вас сейчас нет.")]
        return [
            Reply(
                texts.shift_text(s),
                [[("Записаться", callbacks.shift("signup", s["id"]))]],
            )
            for s in items[:MAX_ITEMS]
        ]

    return await _safe(run)


# ---------------------------------------------------------------------------
# Кнопки
# ---------------------------------------------------------------------------
async def on_callback(crm: CrmClient, telegram_id: int, data: str | None) -> CallbackResult:
    parsed = callbacks.parse(data)
    if parsed is None:
        return CallbackResult(notice="Кнопка устарела.")
    try:
        return await _handle_callback(crm, telegram_id, parsed)
    except NotLinkedError:
        return CallbackResult(notice=texts.NOT_LINKED, remove_buttons=True)
    except CrmUnavailableError:
        return CallbackResult(notice=texts.UNAVAILABLE)
    except CrmError as exc:
        return CallbackResult(notice=exc.detail, replies=[Reply(exc.detail)], remove_buttons=True)


async def _handle_callback(
    crm: CrmClient, telegram_id: int, cb: callbacks.Callback
) -> CallbackResult:
    if cb.kind == "s":  # запись на смену
        a = await crm.signup(telegram_id, cb.id)
        return CallbackResult(
            notice="Вы записаны!",
            replies=[
                Reply(
                    "Вы записаны:\n" + texts.assignment_text(a) + "\n\n"
                    "За день до начала придёт напоминание.",
                    assignment_buttons(a) or None,
                )
            ],
            remove_buttons=True,
        )
    if cb.action == "details":
        a = await crm.assignment(telegram_id, cb.id)
        return CallbackResult(replies=[Reply(texts.assignment_details(a))])
    if cb.action == "confirm":
        await crm.confirm(telegram_id, cb.id)
        return CallbackResult(
            notice="Спасибо!", replies=[Reply("Спасибо, ждём вас!")], remove_buttons=True
        )
    if cb.action == "decline":
        await crm.decline(telegram_id, cb.id)
        return CallbackResult(
            notice="Отказ принят",
            replies=[Reply("Поняли, вы не сможете. Координатор получит уведомление.")],
            remove_buttons=True,
        )
    if cb.action == "attended":
        a = await crm.attendance(telegram_id, cb.id, attended=True)
        return CallbackResult(
            notice="Отмечено",
            replies=[
                Reply(f"Отмечено: {texts.format_hours(a.get('hours'))} ч. Спасибо за помощь!")
            ],
            remove_buttons=True,
        )
    if cb.action == "noshow":
        await crm.attendance(telegram_id, cb.id, attended=False)
        return CallbackResult(
            notice="Отмечено",
            replies=[Reply("Отметили, что вы не смогли прийти.")],
            remove_buttons=True,
        )
    # «Другое число часов»: ждём число следующим сообщением
    return CallbackResult(replies=[Reply(texts.ASK_HOURS)], ask_hours_for=cb.id)


async def submit_hours(
    crm: CrmClient, telegram_id: int, assignment_id: str, text: str | None
) -> tuple[list[Reply], bool]:
    """Число часов после «Другое число часов». Второе значение — сценарий завершён."""
    hours = texts.parse_hours(text)
    if hours is None:
        return [Reply(texts.BAD_HOURS)], False

    async def run() -> list[Reply]:
        a = await crm.attendance(telegram_id, assignment_id, attended=True, hours=hours)
        return [Reply(f"Отмечено: {texts.format_hours(a.get('hours'))} ч. Спасибо за помощь!")]

    return await _safe(run), True
