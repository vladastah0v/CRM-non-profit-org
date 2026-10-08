"""Сообщения волонтёрам в Telegram: назначение, изменения, напоминания (ФТ-У-01).

Кнопки обрабатывает процесс бота: данные кнопки — `a:<действие>:<id назначения>`
(bot/callbacks.py). О визите к подопечному в сообщении нет ни имени, ни адреса:
их бот показывает по кнопке «Подробнее» и только пока назначение действует (п. 7.2).
"""

from __future__ import annotations

import datetime
import logging

from django.db import transaction
from django.utils import timezone

from apps.notifications import telegram
from apps.notifications.telegram import Button, SendResult
from apps.volunteers.models import VolunteerStatus

from .models import PENDING_STATUSES, Assignment, AssignmentStatus
from .services import ATTENDANCE_WINDOW, REMIND_DAY, REMIND_SOON

logger = logging.getLogger(__name__)

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def callback(action: str, assignment: Assignment) -> str:
    return f"a:{action}:{assignment.pk.hex}"


def format_period(starts: datetime.datetime, ends: datetime.datetime) -> str:
    """«пт, 10.10 с 10:00 до 14:00» — во времени фонда."""
    start, end = timezone.localtime(starts), timezone.localtime(ends)
    text = f"{WEEKDAYS[start.weekday()]}, {start:%d.%m} с {start:%H:%M} до {end:%H:%M}"
    if end.date() != start.date():
        text += f" ({end:%d.%m})"
    return text


def format_hours(value: object) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def describe(assignment: Assignment) -> str:
    """Что и когда — без персональных данных подопечного."""
    when = format_period(assignment.starts_at, assignment.ends_at)
    shift = assignment.shift
    if shift is None:
        return f"Визит к подопечному, {when}"
    return f"Смена «{shift.title}», {when}\nМесто: {shift.address}"


def answer_buttons(assignment: Assignment) -> list[list[Button]]:
    row: list[Button] = []
    if assignment.status == AssignmentStatus.ASSIGNED:
        row.append(("Приду", callback("confirm", assignment)))
    row.append(("Не смогу", callback("decline", assignment)))
    rows = [row]
    if assignment.is_visit:
        rows.append([("Подробнее о визите", callback("details", assignment))])
    return rows


def attendance_buttons(assignment: Assignment) -> list[list[Button]]:
    hours = format_hours(assignment.default_hours)
    return [
        [("Был(а), " + hours + " ч", callback("attended", assignment))],
        [("Другое число часов", callback("hours", assignment))],
        [("Не смог(ла) прийти", callback("noshow", assignment))],
    ]


def event_message(
    assignment: Assignment, event: str, count: int = 1
) -> tuple[str, list[list[Button]] | None]:
    text = describe(assignment)
    if event == "assigned":
        if assignment.is_visit and count > 1:
            head = f"Вам назначены регулярные визиты: {count} раз(а), раз в неделю. Первый:"
        elif assignment.is_visit:
            head = "Вам назначен визит."
        else:
            head = "Вас записали на смену."
        return f"{head}\n{text}\n\nПодтвердите, пожалуйста, участие.", answer_buttons(assignment)
    if event == "changed":
        return f"Изменилось время.\n{text}", answer_buttons(assignment)
    if event == "cancelled":
        return f"Отменено.\n{text}", None
    raise ValueError(f"Неизвестное событие: {event}")


def _reachable(assignment: Assignment) -> bool:
    volunteer = assignment.volunteer
    return (
        volunteer.telegram_id is not None
        and not volunteer.is_archived
        and volunteer.status != VolunteerStatus.LEFT
    )


def send_event(assignment_id: str, event: str, count: int = 1) -> SendResult | None:
    """Сообщение о назначении, изменении или отмене (вызывается из Celery)."""
    assignment = (
        Assignment.objects.select_related("shift", "volunteer").filter(pk=assignment_id).first()
    )
    if assignment is None or not _reachable(assignment):
        return None
    if event != "cancelled" and (
        assignment.status not in PENDING_STATUSES or assignment.is_archived
    ):
        return None  # пока сообщение ждало очереди, назначение уже изменилось
    text, buttons = event_message(assignment, event, count)
    chat_id = assignment.volunteer.telegram_id
    return None if chat_id is None else telegram.send_message(chat_id, text, buttons)


def _send_marked(
    assignment: Assignment, text: str, buttons: list[list[Button]], *fields: str
) -> bool:
    chat_id = assignment.volunteer.telegram_id
    if chat_id is None:  # исключено выборкой в send_reminders
        return False
    result = telegram.send_message(chat_id, text, buttons)
    if result is SendResult.FAILED:
        return False  # повторим при следующем запуске
    now = timezone.now()
    for name in fields:
        setattr(assignment, name, now)
    assignment.save(update_fields=list(fields))
    return result is SendResult.SENT


def send_reminders(now: datetime.datetime | None = None) -> int:
    """ФТ-У-01: напоминания за 24 и за 2 часа; после окончания — просьба отметить явку.

    Каждое сообщение — один раз (отметки reminded_* / attendance_asked_at). Строки
    блокируются с SKIP LOCKED: параллельный запуск не отправит дубль.
    """
    if not telegram.is_configured():
        return 0
    now = now or timezone.now()
    base = (
        Assignment.objects.active()
        .with_times()
        .filter(
            status__in=PENDING_STATUSES,
            volunteer__telegram_id__isnull=False,
            volunteer__is_archived=False,
        )
        .exclude(volunteer__status=VolunteerStatus.LEFT)
    )
    due_ids = list(
        base.filter(starts__gt=now, starts__lte=now + REMIND_DAY)
        .filter(reminded_day_at__isnull=True)
        .values_list("pk", flat=True)
    )
    soon_ids = list(
        base.filter(starts__gt=now, starts__lte=now + REMIND_SOON)
        .filter(reminded_soon_at__isnull=True)
        .values_list("pk", flat=True)
    )
    after_ids = list(
        base.filter(ends__lte=now, ends__gt=now - ATTENDANCE_WINDOW)
        .filter(attendance_asked_at__isnull=True)
        .values_list("pk", flat=True)
    )
    sent = 0
    for pk in dict.fromkeys([*soon_ids, *due_ids, *after_ids]):
        with transaction.atomic():
            assignment = (
                Assignment.objects.select_for_update(skip_locked=True, of=("self",))
                .select_related("shift", "volunteer")
                .filter(pk=pk)
                .first()
            )
            if assignment is None:
                continue
            sent += _remind_one(assignment, now)
    return sent


def _remind_one(assignment: Assignment, now: datetime.datetime) -> int:
    starts, ends = assignment.starts_at, assignment.ends_at
    text = describe(assignment)
    if ends <= now:
        if assignment.attendance_asked_at is not None:
            return 0
        message = f"Закончилось:\n{text}\n\nОтметьте, пожалуйста, участие и часы."
        return int(
            _send_marked(assignment, message, attendance_buttons(assignment), "attendance_asked_at")
        )
    if starts <= now:
        return 0
    if starts - now <= REMIND_SOON and assignment.reminded_soon_at is None:
        message = f"Скоро начало — через 2 часа или меньше.\n{text}"
        fields = ["reminded_soon_at"]
        if assignment.reminded_day_at is None:
            fields.append("reminded_day_at")
        return int(_send_marked(assignment, message, answer_buttons(assignment), *fields))
    if starts - now <= REMIND_DAY and assignment.reminded_day_at is None:
        message = f"Напоминаем:\n{text}"
        if assignment.status == AssignmentStatus.ASSIGNED:
            message += "\n\nВы придёте?"
        return int(_send_marked(assignment, message, answer_buttons(assignment), "reminded_day_at"))
    return 0
