"""Тексты бота. Время приходит из CRM уже в часовом поясе фонда (ISO 8601 со смещением)."""

from __future__ import annotations

import datetime
import re
from decimal import Decimal, InvalidOperation
from typing import Any

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

MENU_SHIFTS = "Мои смены"
MENU_SIGNUP = "Записаться на смену"
MENU_HELP = "Помощь"

HELP = (
    "Что умеет бот:\n"
    f"• «{MENU_SHIFTS}» — ближайшие смены и визиты, подтверждение участия;\n"
    f"• «{MENU_SIGNUP}» — открытые смены по вашим навыкам и району;\n"
    "• после смены бот попросит отметить участие и часы.\n\n"
    "За день и за 2 часа до начала придёт напоминание.\n"
    "/unlink — отвязать Telegram от карточки волонтёра."
)
ASK_CODE = (
    "Здравствуйте! Это бот волонтёров фонда.\n\n"
    "Чтобы начать, отправьте код привязки — его выдаёт координатор, "
    "например: ABCD-EFGH."
)
NOT_LINKED = "Telegram не привязан к карточке волонтёра. Отправьте код привязки от координатора."
UNAVAILABLE = "Сервис временно недоступен. Попробуйте позже."
ASK_HOURS = "Сколько часов вы провели на смене? Например: 3,5"
BAD_HOURS = "Не понял число. Напишите часы цифрами — от 0,25 до 24, например: 4 или 2,5"

# Код привязки: 8 символов, можно с дефисом или пробелом посередине
CODE_RE = re.compile(r"^\s*[A-Za-z0-9]{4}[-\s]?[A-Za-z0-9]{4}\s*$")


def looks_like_code(text: str | None) -> bool:
    return bool(text and CODE_RE.match(text))


def parse_time(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


def format_period(starts_at: str, ends_at: str) -> str:
    """«пт, 10.10 с 10:00 до 14:00»."""
    start, end = parse_time(starts_at), parse_time(ends_at)
    text = f"{WEEKDAYS[start.weekday()]}, {start:%d.%m} с {start:%H:%M} до {end:%H:%M}"
    if end.date() != start.date():
        text += f" ({end:%d.%m})"
    return text


def format_hours(value: str | Decimal | None) -> str:
    if value is None:
        return ""
    number = Decimal(value).normalize()
    text = f"{number:f}"
    return text.replace(".", ",")


def parse_hours(text: str | None) -> Decimal | None:
    """Часы от 0,25 до 24 с шагом не мельче 0,01. Неверное значение — None."""
    raw = (text or "").strip().replace(",", ".").removesuffix("ч").strip()
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite() or not Decimal("0.25") <= value <= Decimal(24):
        return None
    return value.quantize(Decimal("0.01"))


def assignment_text(a: dict[str, Any]) -> str:
    lines = [a["title"], format_period(a["starts_at"], a["ends_at"])]
    if a.get("address"):
        lines.append(f"Место: {a['address']}")
    lines.append(f"Статус: {a['status_display'].lower()}")
    if a.get("hours"):
        lines.append(f"Часы: {format_hours(a['hours'])}")
    if a.get("needs_attendance"):
        lines.append("Отметьте, пожалуйста, участие.")
    return "\n".join(lines)


def assignment_details(a: dict[str, Any]) -> str:
    lines = [assignment_text(a)]
    if a.get("description"):
        lines.append(f"\nЧто делать: {a['description']}")
    if a["kind"] == "visit":
        visit = a.get("visit")
        if visit:
            lines.append(f"\nПодопечный: {visit['name']}")
            if visit.get("address"):
                lines.append(f"Адрес: {visit['address']}")
            if visit.get("phone"):
                lines.append(f"Телефон: {visit['phone']}")
            if visit.get("comment"):
                lines.append(f"Задание: {visit['comment']}")
        else:
            lines.append("\nДанные подопечного доступны только на время предстоящего визита.")
    return "\n".join(lines)


def shift_text(s: dict[str, Any]) -> str:
    lines = [f"{s['title']} ({s['type'].lower()})", format_period(s["starts_at"], s["ends_at"])]
    place = s["address"] + (f", {s['district']}" if s.get("district") else "")
    lines.append(f"Место: {place}")
    if s.get("description"):
        lines.append(f"Что делать: {s['description']}")
    lines.append(f"Свободных мест: {s['free_places']}")
    return "\n".join(lines)


def me_text(me: dict[str, Any]) -> str:
    hours = format_hours(me["hours_total"])
    text = f"Здравствуйте, {me['first_name']}!\nВсего отработано часов: {hours}."
    if not me.get("can_sign_up"):
        text += f"\nСтатус в фонде: {me['status_display'].lower()} — запись на смены недоступна."
    return text
