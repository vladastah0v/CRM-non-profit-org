from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django import forms
from django.db.models import Case, IntegerField, Prefetch, QuerySet, Sum, Value, When
from django.http import QueryDict

from apps.accounts.roles import RoleCode, user_has_role
from apps.beneficiaries.models import Beneficiary, CaseStatus
from apps.consents.files import make_download_url
from apps.core.forms import StyledFormMixin
from apps.core.models import BeneficiaryCategory, Program
from apps.core.normalize import normalize_name

from . import permissions
from .models import OPEN_STATUSES, Assistance, AssistanceFile, HelpRequest

RECENT_LIMIT = 20


@dataclass
class BeneficiaryHelp:
    """Блок карточки подопечного: заявки и оказанная помощь."""

    manages: bool
    requests: Sequence[HelpRequest] = field(default_factory=list)
    assistance: Sequence[Assistance] = field(default_factory=list)
    total_amount: Decimal = Decimal(0)
    open_count: int = 0
    # Файлы к помощи (ФТ-З-06) — только тем, кто ведёт подопечного: {pk помощи: [(файл, ссылка)]}
    files: dict[Any, list[tuple[AssistanceFile, str]]] = field(default_factory=dict)


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
    manages = permissions.manages_beneficiary(user, beneficiary)
    if manages:
        assistance = assistance.prefetch_related(
            Prefetch(
                "files",
                queryset=AssistanceFile.objects.filter(is_archived=False).select_related("file"),
            )
        )
    assistance_list = list(assistance[:RECENT_LIMIT])
    files = (
        {
            a.pk: [(f, make_download_url(f.file, user)) for f in a.files.all()]
            for a in assistance_list
        }
        if manages
        else {}
    )
    return BeneficiaryHelp(
        manages=manages,
        requests=request_list,
        assistance=assistance_list,
        total_amount=total,
        open_count=sum(1 for r in request_list if r.is_open),
        files=files,
    )


# ---------------------------------------------------------------------------
# Массовая регистрация помощи (ФТ-З-05)
# ---------------------------------------------------------------------------
BULK_LIMIT = 300  # больше галочек на одной странице — уточните отбор


class BulkFilterForm(StyledFormMixin, forms.Form):
    q = forms.CharField(label="ФИО", required=False)
    program = forms.ModelChoiceField(
        label="Программа", queryset=Program.objects.active(), required=False
    )
    category = forms.ModelChoiceField(
        label="Категория", queryset=BeneficiaryCategory.objects.active(), required=False
    )


@dataclass
class BulkCandidates:
    filter_form: BulkFilterForm
    queryset: QuerySet[Beneficiary]
    total: int

    @property
    def too_many(self) -> bool:
        return self.total > BULK_LIMIT


def bulk_candidates(user: Any, params: QueryDict) -> BulkCandidates:
    """Подопечные, которым сотрудник регистрирует помощь: администратор — любые, куратор —
    свои; карточка не в архиве, случай не закрыт."""
    base = Beneficiary.objects.filter(is_archived=False).exclude(status=CaseStatus.CLOSED)
    if not user_has_role(user, RoleCode.ADMIN):
        base = base & Beneficiary.objects.owned_by(user)
    filter_form = BulkFilterForm(params or None)
    queryset = base
    if filter_form.is_valid():
        data = filter_form.cleaned_data
        for word in normalize_name(data.get("q") or "").split():
            queryset = queryset.filter(search_name__contains=word)
        if data.get("program"):
            queryset = queryset.filter(programs=data["program"])
        if data.get("category"):
            queryset = queryset.filter(category=data["category"])
    queryset = queryset.distinct().order_by("last_name", "first_name", "middle_name")
    return BulkCandidates(filter_form, queryset, queryset.count())
