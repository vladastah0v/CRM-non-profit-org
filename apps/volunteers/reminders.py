"""Напоминания координаторам (Celery Beat): срок документа волонтёра (ФТ-В-09, ФТ-У-04)
и волонтёры без смен (ФТ-У-05). В тексте — без ФИО, только вид документа и ссылка."""

from __future__ import annotations

import datetime

from django.urls import reverse
from django.utils import timezone

from apps.core.dates import EXPIRY_NOTICE_DAYS
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify

from . import activity
from .models import Volunteer, VolunteerDocument, VolunteerStatus
from .services import _coordinators


def send_document_reminders(today: datetime.date | None = None) -> int:
    """За 30 дней до конца срока и по истечении — одно напоминание на срок. Заменённые
    документы, выбывшие, архивные и обезличенные карточки — без напоминаний."""
    today = today or timezone.localdate()
    horizon = today + datetime.timedelta(days=EXPIRY_NOTICE_DAYS)
    due = (
        VolunteerDocument.objects.current()
        .filter(
            valid_until__lte=horizon,
            volunteer__is_archived=False,
            volunteer__anonymized_at__isnull=True,
        )
        .exclude(volunteer__status=VolunteerStatus.LEFT)
        .select_related("document_type", "volunteer")
    )
    recipients = None
    count = 0
    for document in due:
        if document.valid_until is None or document.expiry_notice_for == document.valid_until:
            continue
        recipients = recipients if recipients is not None else _coordinators()
        expired = document.valid_until < today
        notify(
            recipients,
            NotificationKind.DOCUMENT_EXPIRING,
            "Истёк срок документа волонтёра" if expired else "Истекает срок документа волонтёра",
            body=f"{document.document_type}. Действует до {document.valid_until:%d.%m.%Y}.",
            url=document.volunteer.get_absolute_url(),
        )
        # update() — без записи в историю: это служебная отметка
        VolunteerDocument.objects.filter(pk=document.pk).update(
            expiry_notice_for=document.valid_until
        )
        count += 1
    return count


def send_idle_digest(now: datetime.datetime | None = None) -> int:
    """Раз в неделю — сводка: сколько активных волонтёров без смен больше 60 дней.
    Одно уведомление со ссылкой на список, а не по уведомлению на каждого волонтёра."""
    total = activity.idle(Volunteer.objects.filter(is_archived=False), now).count()
    if not total:
        return 0
    notify(
        _coordinators(),
        NotificationKind.VOLUNTEERS_IDLE,
        f"Волонтёры без смен больше {activity.IDLE_DAYS} дней: {total}",
        body="Активные волонтёры, которые давно не были на сменах. Предложите им смену.",
        url=f"{reverse('volunteers:list')}?last_shift=idle",
    )
    return total
