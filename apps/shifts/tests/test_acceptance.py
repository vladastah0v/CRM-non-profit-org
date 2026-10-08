"""Приёмочный сценарий 9.2 ТЗ целиком, через настоящий HTTP:
«Координатор создаёт смену; волонтёр записывается через бота, получает напоминание,
подтверждает явку; часы попадают в карточку».

Бот — тот же код, что в процессе бота (bot.logic + bot.crm), CRM — живой сервер Django.
Telegram подменён: перехватываются сообщения, которые Celery отправил бы волонтёру.
"""

import asyncio
from decimal import Decimal
from unittest import mock

import pytest

from apps.accounts.tokens import issue_token
from apps.core.context import acting_as
from apps.notifications import telegram
from apps.notifications.telegram import SendResult
from apps.shifts import reminders
from apps.shifts.models import AssignmentStatus
from apps.volunteers import services as volunteer_services
from bot import logic
from bot.crm import CrmClient

from .conftest import DAY, HOUR, past_shift

TG = 700001


@pytest.fixture(scope="module", autouse=True)
def _flush_with_audit_trigger_paused():
    """После транзакционного теста pytest-django очищает базу (TRUNCATE), а журнал аудита
    защищён от очистки триггером (НФТ-ИБ-07). Триггер выключается только на время этой
    очистки и сразу включается обратно."""
    from django.db.backends.postgresql.operations import DatabaseOperations

    original = DatabaseOperations.execute_sql_flush
    trigger = "ALTER TABLE audit_auditlog {} TRIGGER audit_log_no_truncate"

    def execute_sql_flush(self, sql_list):
        with self.connection.cursor() as cursor:
            cursor.execute(trigger.format("DISABLE"))
        try:
            original(self, sql_list)
        finally:
            with self.connection.cursor() as cursor:
                cursor.execute(trigger.format("ENABLE"))

    with mock.patch.object(DatabaseOperations, "execute_sql_flush", execute_sql_flush):
        yield


def bot_call(live_server, token, scenario):
    async def main():
        crm = CrmClient(f"{live_server.url}/api/v1/bot/", token)
        try:
            return await scenario(crm)
        finally:
            await crm.close()

    return asyncio.run(main())


# Справочники из миграций (роли и т. п.) после очистки восстанавливаются
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_acceptance_shift_signup_reminder_confirm_hours(
    live_server, settings, coordinator, make_shift, make_active, refs
):
    settings.TELEGRAM_BOT_TOKEN = "123:test"
    _, token = issue_token("Telegram-бот", ["bot"])

    # Координатор создаёт смену и выдаёт волонтёру код привязки
    with acting_as(coordinator):
        shift = make_shift(starts_in=3 * DAY, skills=[refs["skill_drive"]])
    volunteer = make_active(skills=[refs["skill_drive"]])
    code = volunteer_services.issue_bot_link_code(volunteer)

    # Волонтёр привязывает Telegram и записывается на смену через бота
    (greeting,) = bot_call(live_server, token, lambda crm: logic.start(crm, TG, code))
    assert greeting.text.startswith("Готово, Анна!")
    (offer,) = bot_call(live_server, token, lambda crm: logic.open_shifts(crm, TG))
    signup_button = offer.buttons[0][0][1]
    result = bot_call(live_server, token, lambda crm: logic.on_callback(crm, TG, signup_button))
    assert result.notice == "Вы записаны!"
    assignment = shift.assignments.get(volunteer=volunteer)
    assert assignment.status == AssignmentStatus.ASSIGNED

    # За сутки Celery присылает напоминание с кнопкой «Приду»; волонтёр её нажимает
    with mock.patch.object(telegram, "send_message", return_value=SendResult.SENT) as send:
        assert reminders.send_reminders(now=shift.starts_at - 20 * HOUR) == 1
    chat_id, text, buttons = send.call_args.args
    assert chat_id == TG and "Вы придёте?" in text
    confirm_button = buttons[0][0][1]
    result = bot_call(live_server, token, lambda crm: logic.on_callback(crm, TG, confirm_button))
    assert result.notice == "Спасибо!"
    assignment.refresh_from_db()
    assert assignment.status == AssignmentStatus.CONFIRMED

    # После смены — просьба отметить участие; волонтёр подтверждает явку и часы
    past_shift(shift)
    with mock.patch.object(telegram, "send_message", return_value=SendResult.SENT) as send:
        assert reminders.send_reminders() == 1
    attended_button = send.call_args.args[2][0][0][1]
    result = bot_call(live_server, token, lambda crm: logic.on_callback(crm, TG, attended_button))
    assert result.replies[0].text.startswith("Отмечено: 4 ч")

    # Часы попали в карточку волонтёра
    assignment.refresh_from_db()
    assert assignment.status == AssignmentStatus.ATTENDED
    assert volunteer_services.total_hours(volunteer) == Decimal("4.00")
    assert [(c.to_status, c.via_bot) for c in assignment.status_changes.order_by("created_at")] == [
        (AssignmentStatus.ASSIGNED, True),
        (AssignmentStatus.CONFIRMED, True),
        (AssignmentStatus.ATTENDED, True),
    ]
