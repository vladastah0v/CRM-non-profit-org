"""ФТ-У-01: напоминания волонтёру через бота за 24 и 2 часа; отметка явки после смены."""

import json
import urllib.error
from email.message import Message
from unittest import mock

import pytest

from apps.notifications import telegram
from apps.notifications.telegram import SendResult
from apps.shifts import reminders, services
from apps.shifts.models import Assignment, AssignmentStatus
from bot import callbacks as bot_callbacks

from .conftest import DAY, HOUR, NOW, past_shift


@pytest.fixture
def bot_token(settings):
    settings.TELEGRAM_BOT_TOKEN = "123:test"


@pytest.fixture
def linked(make_active):
    counter = {"n": 1000}

    def factory(**fields):
        counter["n"] += 1
        volunteer = make_active(**fields)
        type(volunteer).objects.filter(pk=volunteer.pk).update(telegram_id=counter["n"])
        volunteer.refresh_from_db()
        return volunteer

    return factory


def sent_texts(send: mock.Mock) -> list[str]:
    return [call.args[1] for call in send.call_args_list]


def test_fty01_day_and_soon_reminders_once(bot_token, make_shift, linked):
    shift = make_shift(starts_in=3 * DAY)
    assignment = services.assign_to_shift(shift, linked())
    with mock.patch.object(telegram, "send_message", return_value=SendResult.SENT) as send:
        assert reminders.send_reminders(now=shift.starts_at - 30 * HOUR) == 0
        assert reminders.send_reminders(now=shift.starts_at - 20 * HOUR) == 1
        assert reminders.send_reminders(now=shift.starts_at - 19 * HOUR) == 0  # уже было
        assert reminders.send_reminders(now=shift.starts_at - HOUR) == 1
        assert reminders.send_reminders(now=shift.starts_at - HOUR / 2) == 0
    texts = sent_texts(send)
    assert texts[0].startswith("Напоминаем")
    assert "Вы придёте?" in texts[0]
    assert texts[1].startswith("Скоро начало")
    buttons = send.call_args_list[0].args[2]
    assert [label for label, _ in buttons[0]] == ["Приду", "Не смогу"]
    assignment.refresh_from_db()
    assert assignment.reminded_day_at and assignment.reminded_soon_at


def test_fty01_attendance_request_after_end(bot_token, make_shift, linked):
    shift = make_shift()
    services.assign_to_shift(shift, linked())
    past_shift(shift, started_ago=6 * HOUR)
    with mock.patch.object(telegram, "send_message", return_value=SendResult.SENT) as send:
        assert reminders.send_reminders() == 1
        assert reminders.send_reminders() == 0
    labels = [row[0][0] for row in send.call_args.args[2]]
    assert labels == ["Был(а), 4 ч", "Другое число часов", "Не смог(ла) прийти"]


def test_fty01_failed_send_is_retried_unreachable_is_not(bot_token, make_shift, linked):
    shift = make_shift(starts_in=10 * HOUR)
    assignment = services.assign_to_shift(shift, linked())
    Assignment.objects.filter(pk=assignment.pk).update(reminded_day_at=None)
    with mock.patch.object(telegram, "send_message", return_value=SendResult.FAILED):
        assert reminders.send_reminders() == 0
    assignment.refresh_from_db()
    assert assignment.reminded_day_at is None
    with mock.patch.object(telegram, "send_message", return_value=SendResult.UNREACHABLE):
        reminders.send_reminders()
    assignment.refresh_from_db()
    assert assignment.reminded_day_at is not None  # заблокировал бота — не повторяем


def test_fty01_skips_unlinked_declined_and_unconfigured(settings, make_shift, make_active, linked):
    settings.TELEGRAM_BOT_TOKEN = ""
    shift = make_shift(starts_in=10 * HOUR)
    services.assign_to_shift(shift, make_active())  # без Telegram
    declined = services.assign_to_shift(shift, linked())
    services.change_assignment_status(declined, AssignmentStatus.DECLINED)
    with mock.patch.object(telegram, "send_message") as send:
        assert reminders.send_reminders() == 0  # бот не настроен
        settings.TELEGRAM_BOT_TOKEN = "123:test"
        assert reminders.send_reminders() == 0
    send.assert_not_called()


def test_visit_messages_have_no_beneficiary_data(bot_token, in_work_beneficiary, linked):
    starts = (NOW() + 3 * DAY).replace(minute=0, second=0, microsecond=0)
    (visit,) = services.create_visits(
        in_work_beneficiary, linked(), starts_at=starts, ends_at=starts + 2 * HOUR
    )
    text, buttons = reminders.event_message(visit, "assigned")
    assert buttons is not None
    beneficiary = in_work_beneficiary
    for value in (beneficiary.first_name, beneficiary.last_name, beneficiary.address):
        assert value not in text
    assert [label for label, _ in buttons[1]] == ["Подробнее о визите"]


def test_callback_format_matches_bot(make_shift, make_active):
    assignment = services.assign_to_shift(make_shift(), make_active())
    data = reminders.callback("confirm", assignment)
    assert len(data.encode()) <= 64
    parsed = bot_callbacks.parse(data)
    assert parsed is not None
    assert (parsed.kind, parsed.action, parsed.id) == ("a", "confirm", str(assignment.pk))


def test_send_event_skips_changed_assignment(bot_token, make_shift, linked):
    assignment = services.assign_to_shift(make_shift(), linked())
    with mock.patch.object(telegram, "send_message", return_value=SendResult.SENT) as send:
        assert reminders.send_event(str(assignment.pk), "assigned") is SendResult.SENT
        services.change_assignment_status(assignment, AssignmentStatus.DECLINED)
        assert reminders.send_event(str(assignment.pk), "assigned") is None
    assert send.call_count == 1
    assert send.call_args.args[1].startswith("Вас записали на смену")


# ---------------------------------------------------------------------------
# Отправка через Telegram Bot API
# ---------------------------------------------------------------------------
class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b'{"ok": true}'


def test_telegram_send_message_payload(bot_token):
    with mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
        result = telegram.send_message(42, "Текст", [[("Приду", "a:confirm:1")]])
    assert result is SendResult.SENT
    request = urlopen.call_args.args[0]
    assert request.full_url == "https://api.telegram.org/bot123:test/sendMessage"
    payload = json.loads(request.data)
    assert payload["chat_id"] == 42
    assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "a:confirm:1"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (urllib.error.HTTPError("u", 403, "Forbidden", Message(), None), SendResult.UNREACHABLE),
        (urllib.error.HTTPError("u", 502, "Bad Gateway", Message(), None), SendResult.FAILED),
        (urllib.error.URLError("timeout"), SendResult.FAILED),
    ],
)
def test_telegram_send_message_errors(bot_token, error, expected):
    with mock.patch("urllib.request.urlopen", side_effect=error):
        assert telegram.send_message(42, "Текст") is expected


def test_telegram_not_configured(settings):
    settings.TELEGRAM_BOT_TOKEN = ""
    with mock.patch("urllib.request.urlopen") as urlopen:
        assert telegram.send_message(42, "Текст") is SendResult.FAILED
    urlopen.assert_not_called()


@pytest.mark.parametrize("result", [SendResult.SENT, SendResult.UNREACHABLE, None])
def test_event_task_does_not_retry_terminal_results(result):
    from apps.shifts.tasks import notify_volunteer

    with (
        mock.patch.object(reminders, "send_event", return_value=result),
        mock.patch.object(notify_volunteer, "retry") as retry,
    ):
        notify_volunteer.run("assignment", "assigned")
    retry.assert_not_called()


def test_event_task_retries_temporary_failure():
    from celery.exceptions import Retry

    from apps.shifts.tasks import notify_volunteer

    with (
        mock.patch.object(reminders, "send_event", return_value=SendResult.FAILED),
        mock.patch.object(notify_volunteer, "retry", side_effect=Retry()) as retry,
        pytest.raises(Retry),
    ):
        notify_volunteer.run("assignment", "assigned")
    retry.assert_called_once_with(countdown=30)
