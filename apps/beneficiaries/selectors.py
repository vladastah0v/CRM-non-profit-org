"""Выборки для списка и карточки подопечного — только через visible_to(user) (НФТ-ИБ-08)."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from django.core.exceptions import FieldDoesNotExist, PermissionDenied
from django.db.models import QuerySet
from django.http import QueryDict
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.accounts.roles import RoleCode, user_has_role
from apps.consents.models import Consent

from . import permissions
from .filters import BeneficiaryFilter
from .models import (
    Beneficiary,
    BeneficiaryDocument,
    BeneficiarySensitive,
    CaseStatus,
    CaseStatusChange,
    CuratorAccess,
    RelatedPerson,
)
from .services import allowed_transitions, has_valid_consent, in_work_problems

TIMELINE_LIMIT = 100


def beneficiary_list(user: Any, params: QueryDict) -> BeneficiaryFilter:
    queryset = Beneficiary.objects.visible_to(user).select_related("category", "curator")
    # Полные фильтры — у ролей с полным доступом хотя бы к своим карточкам
    full = user_has_role(user, RoleCode.ADMIN, RoleCode.CURATOR)
    return BeneficiaryFilter(
        params, queryset=queryset, full_access=full, choose_curator=permissions.is_admin(user)
    )


def get_beneficiary(user: Any, pk: Any) -> Beneficiary:
    """Карточка с проверкой прав: чужая или недоступная — 404, как несуществующая."""
    queryset: QuerySet[Beneficiary] = Beneficiary.objects.visible_to(user).select_related(
        "category", "curator"
    )
    return get_object_or_404(queryset, pk=pk)


def get_managed_beneficiary(user: Any, pk: Any) -> Beneficiary:
    """Карточка для изменения: администратор или куратор своего подопечного.

    Чужая карточка — 404 (не раскрываем, что она есть); роль без права изменения — 403.
    """
    if not permissions.can_create(user):
        raise PermissionDenied
    beneficiary = get_beneficiary(user, pk)
    if not permissions.manages(user, beneficiary):
        raise PermissionDenied
    return beneficiary


@dataclass
class TimelineEvent:
    at: datetime.datetime
    title: str
    details: str = ""
    author: Any = None


@dataclass
class BeneficiaryCard:
    beneficiary: Beneficiary
    full: bool
    is_admin: bool
    related_persons: list[RelatedPerson] = field(default_factory=list)
    consents: list[Consent] = field(default_factory=list)
    shared_with: list[CuratorAccess] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)
    in_work_problems: list[str] = field(default_factory=list)
    consent_missing: bool = False

    @property
    def show_in_work_hint(self) -> bool:
        return bool(self.in_work_problems) and CaseStatus.IN_WORK in self.transitions


def beneficiary_card(user: Any, beneficiary: Beneficiary) -> BeneficiaryCard:
    if not permissions.manages(user, beneficiary):
        return BeneficiaryCard(beneficiary, full=False, is_admin=False)
    consents = list(
        Consent.objects.visible_to(user)
        .filter(beneficiary=beneficiary, is_archived=False)
        .select_related("scan")
    )
    return BeneficiaryCard(
        beneficiary=beneficiary,
        full=True,
        is_admin=permissions.is_admin(user),
        related_persons=list(beneficiary.related_persons.filter(is_archived=False)),
        consents=consents,
        shared_with=list(
            CuratorAccess.objects.filter(beneficiary=beneficiary, is_archived=False).select_related(
                "user", "created_by"
            )
        ),
        timeline=timeline(beneficiary),
        transitions=[] if beneficiary.is_archived else allowed_transitions(beneficiary),
        in_work_problems=in_work_problems(beneficiary),
        consent_missing=beneficiary.status == CaseStatus.IN_WORK
        and not has_valid_consent(beneficiary),
    )


# ---------------------------------------------------------------------------
# Лента истории (ФТ-П-05)
# ---------------------------------------------------------------------------
def _field_label(model: type, name: str) -> str:
    try:
        return str(model._meta.get_field(name).verbose_name)  # type: ignore[attr-defined]
    except FieldDoesNotExist:
        return name


def _start_of(day: datetime.date) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


def timeline(beneficiary: Beneficiary) -> list[TimelineEvent]:
    """Все события случая в хронологическом порядке (новые сверху).

    Без значений полей — только что изменилось: значения есть в самой карточке.
    Заявки, помощь и взаимодействия добавятся с модулями этапа 6.
    """
    events: list[TimelineEvent] = []

    for change in CaseStatusChange.objects.filter(beneficiary=beneficiary).select_related(
        "reason", "created_by"
    ):
        title = (
            f"Статус: {change.get_from_status_display()} → {change.get_to_status_display()}"
            if change.from_status
            else "Карточка создана"
        )
        details = "; ".join(p for p in (str(change.reason or ""), change.comment) if p)
        events.append(TimelineEvent(change.created_at, title, details, change.created_by))

    skip = {
        "status",
        "status_changed_at",
        "closed_on",
        "closing_reason",
        "outcome",
        "updated_at",
        "updated_by",
        "created_at",
        "created_by",
    }
    records = list(beneficiary.history.select_related("history_user").order_by("history_date"))
    for previous, record in pairwise(records):
        delta = record.diff_against(previous)
        fields = [c.field for c in delta.changes if c.field not in skip]
        if fields:
            labels = ", ".join(_field_label(Beneficiary, f) for f in fields)
            events.append(
                TimelineEvent(record.history_date, "Изменена карточка", labels, record.history_user)
            )

    sensitive = BeneficiarySensitive.objects.filter(beneficiary=beneficiary).first()
    if sensitive is not None:
        records = list(sensitive.history.select_related("history_user").order_by("history_date"))
        for index, record in enumerate(records):
            if index == 0:
                fields = [
                    f for f in ("health", "disability", "passport", "snils") if getattr(record, f)
                ]
            else:
                fields = [
                    c.field
                    for c in record.diff_against(records[index - 1]).changes
                    if c.field in ("health", "disability", "passport", "snils")
                ]
            if fields:
                labels = ", ".join(_field_label(BeneficiarySensitive, f) for f in fields)
                events.append(
                    TimelineEvent(
                        record.history_date,
                        "Изменены защищённые данные",
                        labels,
                        record.history_user,
                    )
                )

    for consent in Consent.objects.filter(beneficiary=beneficiary).select_related("created_by"):
        events.append(
            TimelineEvent(
                consent.created_at, "Добавлено согласие", str(consent), consent.created_by
            )
        )
        if consent.revoked_on:
            events.append(
                TimelineEvent(_start_of(consent.revoked_on), "Отозвано согласие", str(consent))
            )

    for person in RelatedPerson.objects.filter(beneficiary=beneficiary).select_related(
        "created_by"
    ):
        events.append(
            TimelineEvent(
                person.created_at, "Добавлено связанное лицо", str(person), person.created_by
            )
        )

    for document in BeneficiaryDocument.objects.filter(beneficiary=beneficiary).select_related(
        "created_by"
    ):
        events.append(
            TimelineEvent(
                document.created_at, "Загружен документ", str(document), document.created_by
            )
        )

    for access in CuratorAccess.objects.filter(beneficiary=beneficiary).select_related(
        "user", "created_by"
    ):
        events.append(
            TimelineEvent(
                access.created_at, "Открыт доступ куратору", str(access.user), access.created_by
            )
        )
        if access.archived_at:
            events.append(
                TimelineEvent(
                    access.archived_at,
                    "Закрыт доступ куратору",
                    str(access.user),
                    access.updated_by,
                )
            )

    events.sort(key=lambda event: event.at, reverse=True)
    return events[:TIMELINE_LIMIT]
