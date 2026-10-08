"""Выдача всех данных человека по его запросу (НФТ-ПД-05, ст. 14 152-ФЗ).

Одна книга Excel: лист на каждый вид данных — карточка, защищённый блок, согласия,
связанные лица, заявки, помощь, смены и визиты, часы, взаимодействия, задачи, история
статусов, файлы (только названия: сами файлы выдаются по ссылкам из карточки).
Выгружает только администратор; событие — в журнал аудита (EXPORT и VIEW_SENSITIVE).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from django.db import models

from apps.beneficiaries.models import Beneficiary, BeneficiarySensitive
from apps.core.xlsx import Sheet
from apps.volunteers.models import Volunteer, VolunteerAvailability

from .models import Consent

# Служебные поля, которые не являются данными о человеке
SKIP_FIELDS = frozenset(
    {"id", "search_name", "bot_link_code", "bot_link_code_expires", "is_archived", "user"}
)


def _display(obj: models.Model, field: models.Field) -> Any:
    if field.choices:
        return getattr(obj, f"get_{field.name}_display")()
    value = getattr(obj, field.name)
    if isinstance(field, models.ForeignKey):
        return str(value) if value is not None else ""
    return value


def _fields(model: type[models.Model], skip: Iterable[str] = ()) -> list[models.Field]:
    excluded = SKIP_FIELDS | set(skip)
    return [
        f
        for f in model._meta.concrete_fields
        if f.name not in excluded and not f.name.endswith("_ptr")
    ]


def card_sheet(title: str, obj: models.Model, skip: Iterable[str] = ()) -> Sheet:
    """Вертикальная таблица «поле — значение»."""
    rows = [(str(f.verbose_name).capitalize(), _display(obj, f)) for f in _fields(type(obj), skip)]
    if hasattr(obj, "skills"):
        rows.append(("Навыки", ", ".join(str(s) for s in obj.skills.all())))
    if hasattr(obj, "programs"):
        rows.append(("Программы", ", ".join(str(p) for p in obj.programs.all())))
    return Sheet(title, ["Поле", "Значение"], rows)


def table_sheet(
    title: str,
    queryset: Iterable[models.Model],
    model: type[models.Model],
    skip: Sequence[str] = (),
) -> Sheet:
    """Горизонтальная таблица: строка — запись. Ссылки на саму карточку не дублируются."""
    fields = _fields(model, skip)
    return Sheet(
        title,
        [str(f.verbose_name).capitalize() for f in fields],
        [[_display(obj, f) for f in fields] for obj in queryset],
    )


CARD_LINKS = ("volunteer", "beneficiary", "created_by", "updated_by")


def volunteer_sheets(volunteer: Volunteer) -> list[Sheet]:
    from apps.interactions.models import Interaction, Task
    from apps.requests.models import Assistance
    from apps.shifts.models import Assignment

    sheets = [
        card_sheet("Карточка", volunteer, skip=("photo",)),
        table_sheet("Доступность", volunteer.availability.all(), VolunteerAvailability, CARD_LINKS),
        table_sheet("Согласия", Consent.objects.filter(volunteer=volunteer), Consent, CARD_LINKS),
        table_sheet(
            "Обучение",
            volunteer.trainings.select_related("training_type"),
            volunteer.trainings.model,
            CARD_LINKS,
        ),
        table_sheet(
            "Часы вручную",
            volunteer.manual_hours.select_related("program"),
            volunteer.manual_hours.model,
            CARD_LINKS,
        ),
        table_sheet(
            "Смены и визиты",
            Assignment.objects.filter(volunteer=volunteer).select_related("shift", "beneficiary"),
            Assignment,
            (
                *CARD_LINKS,
                "beneficiary",
                "reminded_day_at",
                "reminded_soon_at",
                "attendance_asked_at",
            ),
        ),
        table_sheet(
            "Оказанная помощь",
            Assistance.objects.filter(executor_volunteer=volunteer).select_related(
                "assistance_type", "program"
            ),
            Assistance,
            (
                *CARD_LINKS,
                "beneficiary",
                "request",
                "executor_user",
                "executor_volunteer",
                "comment",
            ),
        ),
        table_sheet(
            "Взаимодействия",
            Interaction.objects.filter(volunteer=volunteer),
            Interaction,
            CARD_LINKS,
        ),
        table_sheet("Задачи", Task.objects.filter(volunteer=volunteer), Task, CARD_LINKS),
        table_sheet(
            "История статусов",
            volunteer.status_changes.select_related("reason"),
            volunteer.status_changes.model,
            CARD_LINKS,
        ),
    ]
    sheets.append(
        _files_sheet(
            [c.scan for c in Consent.objects.filter(volunteer=volunteer)] + [volunteer.photo]
        )
    )
    return sheets


def beneficiary_sheets(beneficiary: Beneficiary) -> list[Sheet]:
    from apps.interactions.models import Interaction, Task
    from apps.requests.models import Assistance, HelpRequest
    from apps.shifts.models import Assignment

    sensitive = BeneficiarySensitive.objects.filter(beneficiary=beneficiary).first()
    sheets = [card_sheet("Карточка", beneficiary)]
    if sensitive is not None:
        sheets.append(card_sheet("Защищённые данные", sensitive, skip=CARD_LINKS))
    documents = list(beneficiary.documents.select_related("file"))
    consents = list(Consent.objects.filter(beneficiary=beneficiary).select_related("scan"))
    sheets += [
        table_sheet("Согласия", consents, Consent, CARD_LINKS),
        table_sheet(
            "Связанные лица",
            beneficiary.related_persons.all(),
            beneficiary.related_persons.model,
            CARD_LINKS,
        ),
        table_sheet(
            "Заявки",
            HelpRequest.objects.filter(beneficiary=beneficiary).select_related(
                "assistance_type", "responsible"
            ),
            HelpRequest,
            (*CARD_LINKS, "deadline_notified_on"),
        ),
        table_sheet(
            "Оказанная помощь",
            Assistance.objects.filter(beneficiary=beneficiary).select_related(
                "assistance_type", "program", "executor_user", "executor_volunteer"
            ),
            Assistance,
            (*CARD_LINKS, "request"),
        ),
        table_sheet(
            "Визиты волонтёров",
            Assignment.objects.filter(beneficiary=beneficiary).select_related("volunteer"),
            Assignment,
            (*CARD_LINKS, "shift", "reminded_day_at", "reminded_soon_at", "attendance_asked_at"),
        ),
        table_sheet(
            "Взаимодействия",
            Interaction.objects.filter(beneficiary=beneficiary),
            Interaction,
            CARD_LINKS,
        ),
        table_sheet("Задачи", Task.objects.filter(beneficiary=beneficiary), Task, CARD_LINKS),
        table_sheet(
            "История статусов",
            beneficiary.status_changes.select_related("reason"),
            beneficiary.status_changes.model,
            CARD_LINKS,
        ),
        _files_sheet([c.scan for c in consents] + [d.file for d in documents]),
    ]
    return sheets


def _files_sheet(attachments: Iterable[Any]) -> Sheet:
    rows = [
        (a.original_name, a.content_type, a.size, a.created_at)
        for a in attachments
        if a is not None
    ]
    return Sheet(
        "Файлы",
        ["Имя файла", "Тип", "Размер, байт", "Загружен"],
        rows,
        caption=["Сами файлы выдаются отдельно — по ссылкам из карточки."],
    )
