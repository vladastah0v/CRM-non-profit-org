from __future__ import annotations

from typing import Any, cast

from django import forms

from apps.accounts.models import User
from apps.accounts.roles import RoleCode
from apps.beneficiaries.models import Beneficiary
from apps.core.forms import DateInput, StyledFormMixin
from apps.core.models import AssistanceType, Program
from apps.volunteers.models import Volunteer, VolunteerStatus

from .models import AssistanceFileKind, HelpRequest, RequestStatus, Urgency


def responsibles() -> Any:
    return (
        User.objects.filter(is_active=True, roles__code__in=[RoleCode.CURATOR, RoleCode.ADMIN])
        .distinct()
        .order_by("last_name", "first_name")
    )


class HelpRequestForm(StyledFormMixin, forms.Form):
    """ФТ-З-01: тип помощи, описание, срочность, желаемая дата, ответственный."""

    assistance_type = forms.ModelChoiceField(
        label="Тип помощи", queryset=AssistanceType.objects.none()
    )
    description = forms.CharField(
        label="Описание", widget=forms.Textarea(attrs={"rows": 4}), max_length=5000
    )
    urgency = forms.ChoiceField(label="Срочность", choices=Urgency.choices, initial=Urgency.NORMAL)
    due_date = forms.DateField(
        label="Желаемая дата", required=False, widget=DateInput(), help_text="Срок выполнения."
    )
    responsible = forms.ModelChoiceField(
        label="Ответственный",
        queryset=User.objects.none(),
        required=False,
        help_text="Пусто — куратор подопечного.",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        cast(
            forms.ModelChoiceField, self.fields["assistance_type"]
        ).queryset = AssistanceType.objects.active()
        cast(forms.ModelChoiceField, self.fields["responsible"]).queryset = responsibles()

    @classmethod
    def initial_for(cls, help_request: HelpRequest) -> dict[str, Any]:
        return {
            "assistance_type": help_request.assistance_type_id,
            "description": help_request.description,
            "urgency": help_request.urgency,
            "due_date": help_request.due_date,
            "responsible": help_request.responsible_id,
        }

    def service_data(self) -> dict[str, Any]:
        return {k: v for k, v in self.cleaned_data.items() if k != "responsible"}


class RequestStatusForm(StyledFormMixin, forms.Form):
    to_status = forms.ChoiceField(label="Новый статус")
    reason = forms.CharField(
        label="Причина",
        max_length=500,
        required=False,
        help_text="Обязательна для отклонения и отмены.",
    )
    comment = forms.CharField(label="Комментарий", max_length=500, required=False)

    def __init__(self, *args: Any, transitions: list[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        cast(forms.ChoiceField, self.fields["to_status"]).choices = [
            (status, RequestStatus(status).label) for status in transitions
        ]

    def clean(self) -> dict[str, Any]:
        super().clean()
        data = self.cleaned_data
        needs_reason = data.get("to_status") in (RequestStatus.REJECTED, RequestStatus.CANCELLED)
        if needs_reason and not (data.get("reason") or "").strip():
            self.add_error("reason", "Укажите причину.")
        return data


class AssistanceForm(StyledFormMixin, forms.Form):
    """ФТ-З-03: тип, дата, количество или сумма, исполнитель, программа."""

    request = forms.ModelChoiceField(
        label="По заявке", queryset=HelpRequest.objects.none(), required=False
    )
    assistance_type = forms.ModelChoiceField(
        label="Тип помощи", queryset=AssistanceType.objects.none()
    )
    provided_on = forms.DateField(label="Дата", widget=DateInput())
    quantity = forms.DecimalField(
        label="Количество",
        required=False,
        min_value=0,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "any", "inputmode": "decimal"}),
    )
    unit = forms.CharField(
        label="Единица", max_length=50, required=False, help_text="Пусто — из справочника."
    )
    amount = forms.DecimalField(
        label="Сумма, ₽",
        required=False,
        min_value=0,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "inputmode": "decimal"}),
    )
    executor_user = forms.ModelChoiceField(
        label="Исполнитель-сотрудник", queryset=User.objects.none(), required=False
    )
    executor_volunteer = forms.ModelChoiceField(
        label="Исполнитель-волонтёр",
        queryset=Volunteer.objects.none(),
        required=False,
        help_text="Укажите одного исполнителя: сотрудника или волонтёра.",
    )
    program = forms.ModelChoiceField(
        label="Программа", queryset=Program.objects.none(), required=False
    )
    comment = forms.CharField(
        label="Комментарий", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(
        self,
        *args: Any,
        beneficiary: Beneficiary | None = None,
        with_request: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        querysets: dict[str, Any] = {
            "assistance_type": AssistanceType.objects.active(),
            "program": Program.objects.active(),
            "executor_user": User.objects.filter(
                is_active=True,
                roles__code__in=[RoleCode.ADMIN, RoleCode.COORDINATOR, RoleCode.CURATOR],
            )
            .distinct()
            .order_by("last_name", "first_name"),
            # Только ФИО для выбора: остальные поля волонтёра не загружаются
            "executor_volunteer": Volunteer.objects.filter(
                is_archived=False, status__in=[VolunteerStatus.ACTIVE, VolunteerStatus.PAUSED]
            ).only("id", "last_name", "first_name", "middle_name"),
            "request": HelpRequest.objects.open().filter(beneficiary=beneficiary)
            if beneficiary is not None
            else HelpRequest.objects.none(),
        }
        for name, queryset in querysets.items():
            cast(forms.ModelChoiceField, self.fields[name]).queryset = queryset
        if not with_request:
            del self.fields["request"]

    def clean(self) -> dict[str, Any]:
        super().clean()
        data = self.cleaned_data
        if data.get("quantity") is None and data.get("amount") is None:
            self.add_error("quantity", "Укажите количество или сумму.")
        if (data.get("executor_user") is None) == (data.get("executor_volunteer") is None):
            self.add_error("executor_user", "Укажите одного исполнителя: сотрудника или волонтёра.")
        return data

    def service_data(self) -> dict[str, Any]:
        return {k: v for k, v in self.cleaned_data.items() if k != "request"}


class BeneficiaryChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj: Any) -> str:
        """ФИО и год рождения — чтобы различать тёзок."""
        return f"{obj.full_name}, {obj.birth_date:%Y} г. р."


class BulkAssistanceForm(AssistanceForm):
    """ФТ-З-05: одна и та же помощь сразу нескольким подопечным. Количество и сумма — на
    каждого. batch_id — метка отправки: повтор той же формы записи не задваивает."""

    beneficiaries = BeneficiaryChoiceField(
        label="Подопечные",
        queryset=Beneficiary.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        error_messages={"required": "Отметьте хотя бы одного подопечного."},
    )
    batch_id = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args: Any, candidates: Any, **kwargs: Any) -> None:
        super().__init__(*args, with_request=False, **kwargs)
        field = cast(forms.ModelMultipleChoiceField, self.fields["beneficiaries"])
        field.queryset = candidates
        self.fields["quantity"].help_text = "На каждого подопечного."
        self.fields["amount"].help_text = "На каждого подопечного."

    def service_data(self) -> dict[str, Any]:
        skip = {"request", "beneficiaries", "batch_id"}
        return {k: v for k, v in self.cleaned_data.items() if k not in skip}


class AssistanceFileForm(StyledFormMixin, forms.Form):
    """ФТ-З-06: фото или подтверждающий документ (акт, чек, накладная)."""

    kind = forms.ChoiceField(label="Что это", choices=AssistanceFileKind.choices)
    upload = forms.FileField(
        label="Файл",
        help_text="PDF или фото, до 20 МБ.",
        widget=forms.ClearableFileInput(attrs={"accept": "application/pdf,image/*"}),
    )
    title = forms.CharField(
        label="Описание", max_length=200, required=False, help_text="Например, «акт выдачи»."
    )
