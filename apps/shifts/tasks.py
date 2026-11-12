from typing import Any

from celery import shared_task

from apps.notifications.telegram import SendResult

from . import reminders


@shared_task(name="shifts.notify_volunteer", bind=True, ignore_result=True, max_retries=5)
def notify_volunteer(self: Any, assignment_id: str, event: str, count: int = 1) -> None:
    """Сообщение волонтёру о назначении, изменении или отмене. В очередь попадает
    только id назначения — текст собирается из базы в момент отправки."""
    if reminders.send_event(assignment_id, event, count) is SendResult.FAILED:
        raise self.retry(countdown=min(30 * 2**self.request.retries, 600))


@shared_task(name="shifts.send_reminders")
def send_reminders() -> int:
    return reminders.send_reminders()
