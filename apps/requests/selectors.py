from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.db.models import Case, IntegerField, Sum, Value, When

from apps.beneficiaries.models import Beneficiary

from . import permissions
from .models import OPEN_STATUSES, Assistance, HelpRequest

RECENT_LIMIT = 20


@dataclass
class BeneficiaryHelp:
    """Блок карточки подопечного: заявки и оказанная помощь."""

    manages: bool
    requests: Sequence[HelpRequest] = field(default_factory=list)
    assistance: Sequence[Assistance] = field(default_factory=list)
    total_amount: Decimal = Decimal(0)
    open_count: int = 0


def beneficiary_help(user: Any, beneficiary: Beneficiary) -> BeneficiaryHelp:
    """Заявки и помощь в пределах прав пользователя (координатор — только чтение)."""
    if not permissions.can_view(user):
        return BeneficiaryHelp(manages=False)
    requests = (
        HelpRequest.objects.visible_to(user)
        .filter(beneficiary=beneficiary, is_archived=False)
        .select_related("assistance_type", "responsible")
        .annotate(
            is_open_rank=Case(
                When(status__in=OPEN_STATUSES, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("is_open_rank", "due_date", "-created_at")
    )
    assistance = (
        Assistance.objects.visible_to(user)
        .filter(beneficiary=beneficiary, is_archived=False)
        .select_related("assistance_type", "program", "executor_user", "executor_volunteer")
    )
    total = assistance.aggregate(total=Sum("amount"))["total"] or Decimal(0)
    request_list = list(requests[:RECENT_LIMIT])
    return BeneficiaryHelp(
        manages=permissions.manages_beneficiary(user, beneficiary),
        requests=request_list,
        assistance=list(assistance[:RECENT_LIMIT]),
        total_amount=total,
        open_count=sum(1 for r in request_list if r.is_open),
    )
