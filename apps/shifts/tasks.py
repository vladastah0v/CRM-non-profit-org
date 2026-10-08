from celery import shared_task

from . import reminders


@shared_task(name="shifts.notify_volunteer", ignore_result=True)
def notify_volunteer(assignment_id: str, event: str, count: int = 1) -> None:
    """Сообщение волонтёру о назначении, изменении или отмене. В очередь попадает
    только id назначения — текст собирается из базы в момент отправки."""
    reminders.send_event(assignment_id, event, count)


@shared_task(name="shifts.send_reminders")
def send_reminders() -> int:
    return reminders.send_reminders()
