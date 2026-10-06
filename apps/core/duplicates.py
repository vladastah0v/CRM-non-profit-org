"""Проверка дублей людей в базе: волонтёров (ФТ-В-04) и подопечных (ФТ-П-06).

Модель должна иметь поля `search_name` (нормализованное ФИО, триграммный индекс),
`birth_date` и `phone` (E.164); `email` — если есть у модели.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import Model, Q, QuerySet

from .normalize import normalize_email, normalize_name

# Порог похожести ФИО (pg_trgm) при совпадении даты рождения: ловит опечатки
# («Иванова Мария» / «Иванова Марья»), но не однофамильцев с другим именем.
NAME_SIMILARITY = 0.6


@dataclass
class PersonMatch:
    person: Any
    reasons: list[str] = field(default_factory=list)


def find_person_duplicates(
    queryset: QuerySet[Any],
    *,
    last_name: str,
    first_name: str,
    middle_name: str = "",
    birth_date: datetime.date | None,
    phone: str = "",
    email: str = "",
    exclude_pk: Any = None,
    limit: int = 10,
) -> list[PersonMatch]:
    """Совпадение телефона, email или ФИО с датой рождения. Ищет по переданной выборке —
    обычно по всем записям, включая архивные: дубль архивной карточки — тоже дубль."""
    name = normalize_name(last_name, first_name, middle_name)
    email = normalize_email(email)
    name_matches: set[Any] = set()
    if birth_date is not None and name:
        name_matches = set(
            queryset.filter(birth_date=birth_date)
            .annotate(similarity=TrigramSimilarity("search_name", name))
            .filter(Q(search_name=name) | Q(similarity__gte=NAME_SIMILARITY))
            .values_list("pk", flat=True)
        )
    condition = Q(pk__in=name_matches)
    if phone:
        condition |= Q(phone=phone)
    if email and _has_field(queryset.model, "email"):
        condition |= Q(email=email)

    candidates = queryset.filter(condition)
    if exclude_pk is not None:
        candidates = candidates.exclude(pk=exclude_pk)

    matches = []
    for person in candidates.order_by("-created_at")[:limit]:
        match = PersonMatch(person)
        if phone and person.phone == phone:
            match.reasons.append("телефон")
        if email and getattr(person, "email", None) == email:
            match.reasons.append("email")
        if person.pk in name_matches:
            match.reasons.append("ФИО и дата рождения")
        matches.append(match)
    return matches


def _has_field(model: type[Model], name: str) -> bool:
    return any(f.name == name for f in model._meta.get_fields())
