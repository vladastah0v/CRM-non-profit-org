from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404

from apps.beneficiaries.models import Beneficiary
from apps.volunteers.models import Volunteer

from .models import Interaction, Task, TaskStatus
from .services import Card, can_write

CARD_MODELS: dict[str, type[Volunteer] | type[Beneficiary]] = {
    "volunteer": Volunteer,
    "beneficiary": Beneficiary,
}
RECENT_LIMIT = 20


def get_card(user: Any, kind: str, pk: Any, *, for_write: bool = False) -> Card:
    """Карточка волонтёра или подопечного — только через visible_to(user) (НФТ-ИБ-08)."""
    model = CARD_MODELS.get(kind)
    if model is None:
        raise Http404
    card = cast(Card, get_object_or_404(model.objects.visible_to(user), pk=pk))
    if for_write and not can_write(user, card):
        raise PermissionDenied
    return card


def card_kind(card: Card) -> str:
    return "volunteer" if isinstance(card, Volunteer) else "beneficiary"


@dataclass
class CardActivity:
    kind: str
    can_write: bool
    interactions: list[Interaction] = field(default_factory=list)
    open_tasks: list[Task] = field(default_factory=list)
    closed_tasks: list[Task] = field(default_factory=list)


def card_activity(user: Any, card: Card) -> CardActivity:
    """Взаимодействия и задачи для блока карточки. Видны тем, кто работает с карточкой."""
    kind = card_kind(card)
    writable = can_write(user, card)
    if not writable:
        return CardActivity(kind, False)
    link = {kind: card}
    interactions = (
        Interaction.objects.visible_to(user)
        .filter(is_archived=False, **link)
        .select_related("created_by")[:RECENT_LIMIT]
    )
    tasks = (
        Task.objects.visible_to(user)
        .filter(is_archived=False, **link)
        .select_related("assignee", "created_by")
    )
    return CardActivity(
        kind=kind,
        can_write=True,
        interactions=list(interactions),
        open_tasks=list(tasks.filter(status=TaskStatus.OPEN).order_by("due_date")),
        closed_tasks=list(tasks.exclude(status=TaskStatus.OPEN).order_by("-done_at")[:5]),
    )
