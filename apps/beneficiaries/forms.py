from __future__ import annotations

import datetime
from typing import Any, cast

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.roles import RoleCode
from apps.core.forms import DateInput, StyledFormMixin
from apps.core.models import BeneficiaryCategory, BeneficiarySource, CaseClosingReason, Program
from apps.core.normalize import normalize_phone

from .models import Beneficiary, CaseStatus, DocumentKind, RelatedPerson
from .services import normalize_snils


def curators() -> Any:
    return (
        User.objects.filter(is_active=True, roles__code=RoleCode.CURATOR)
        .distinct()
        .order_by("last_name", "first_name")
    )


class BeneficiaryForm(StyledFormMixin, forms.ModelForm):
    """Карточка подопечного (ФТ-П-01). Сохраняет не форма, а сервис."""

    confirm_duplicates = forms.BooleanField(
        label="Это другой человек — всё равно сохранить", required=False
    )
    curator = forms.ModelChoiceField(
        label="Ответственный куратор", queryset=User.objects.none(), required=False
    )

    class Meta:
        model = Beneficiary
        fields = (
            "last_name",
            "first_name",
            "middle_name",
            "birth_date",
            "address",
            "phone",
            "category",
            "source",
            "programs",
        )
        widgets = {
            "birth_date": DateInput(),
            "phone": forms.TextInput(attrs={"type": "tel", "autocomplete": "off"}),
            "address": forms.TextInput(attrs={"autocomplete": "off"}),
            "programs": forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args: Any, choose_curator: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        references = {
            "category": BeneficiaryCategory.objects.active(),
            "source": BeneficiarySource.objects.active(),
            "programs": Program.objects.active(),
        }
        for name, queryset in references.items():
            cast(forms.ModelChoiceField, self.fields[name]).queryset = queryset
        if choose_curator:
            cast(forms.ModelChoiceField, self.fields["curator"]).queryset = curators()
        else:
            # Куратор, создающий карточку, сам становится ответственным
            del self.fields["curator"]

    def clean_phone(self) -> str:
        return normalize_phone(self.cleaned_data.get("phone", ""))

    def clean_birth_date(self) -> datetime.date:
        birth_date: datetime.date = self.cleaned_data["birth_date"]
        if birth_date > timezone.localdate():
            raise ValidationError("Дата рождения не может быть в будущем.")
        return birth_date

    def service_data(self) -> dict[str, Any]:
        return {key: self.cleaned_data[key] for key in self.Meta.fields if key != "programs"}


class StatusChangeForm(StyledFormMixin, forms.Form):
    to_status = forms.ChoiceField(label="Новый статус")
    reason = forms.ModelChoiceField(
        label="Причина закрытия",
        queryset=CaseClosingReason.objects.active(),
        required=False,
        help_text="Обязательна для статуса «Закрыт».",
    )
    outcome = forms.CharField(
        label="Итог",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Обязателен для статуса «Закрыт»: чем закончилась работа со случаем.",
    )
    comment = forms.CharField(label="Комментарий", max_length=500, required=False)

    def __init__(self, *args: Any, transitions: list[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        cast(forms.ChoiceField, self.fields["to_status"]).choices = [
            (status, CaseStatus(status).label) for status in transitions
        ]

    def clean(self) -> dict[str, Any]:
        super().clean()
        data = self.cleaned_data
        if data.get("to_status") == CaseStatus.CLOSED:
            if not data.get("reason"):
                self.add_error("reason", "Укажите причину закрытия.")
            if not (data.get("outcome") or "").strip():
                self.add_error("outcome", "Опишите итог работы со случаем.")
        return data


class SensitiveForm(StyledFormMixin, forms.Form):
    """Защищённый блок (ФТ-П-03). Здоровье и инвалидность — особые категории ПДн."""

    health = forms.CharField(
        label="Здоровье",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Только то, что нужно для помощи. Нужно согласие на особые категории данных.",
    )
    disability = forms.CharField(
        label="Инвалидность",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Группа, срок. Нужно согласие на особые категории данных.",
    )
    passport = forms.CharField(
        label="Паспорт",
        required=False,
        max_length=300,
        help_text="Серия, номер, кем и когда выдан.",
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    snils = forms.CharField(
        label="СНИЛС",
        required=False,
        max_length=20,
        widget=forms.TextInput(attrs={"autocomplete": "off", "inputmode": "numeric"}),
    )

    def clean_snils(self) -> str:
        try:
            return normalize_snils(self.cleaned_data.get("snils", ""))
        except ValidationError as exc:
            raise ValidationError(exc.message_dict["snils"]) from exc


class DocumentForm(StyledFormMixin, forms.Form):
    kind = forms.ChoiceField(label="Вид документа", choices=DocumentKind.choices)
    title = forms.CharField(label="Описание", max_length=200, required=False)
    upload = forms.FileField(
        label="Файл",
        help_text="PDF, фото, DOCX или XLSX, до 20 МБ. Хранится в зашифрованном виде.",
    )


class RelatedPersonForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = RelatedPerson
        fields = ("full_name", "relation", "phone", "email", "is_legal_representative")
        widgets = {"phone": forms.TextInput(attrs={"type": "tel", "autocomplete": "off"})}

    def clean_phone(self) -> str:
        return normalize_phone(self.cleaned_data.get("phone", ""))


class CuratorForm(StyledFormMixin, forms.Form):
    curator = forms.ModelChoiceField(
        label="Ответственный куратор", queryset=User.objects.none(), required=False
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        cast(forms.ModelChoiceField, self.fields["curator"]).queryset = curators()


class GrantAccessForm(StyledFormMixin, forms.Form):
    user = forms.ModelChoiceField(label="Второй куратор", queryset=User.objects.none())

    def __init__(self, *args: Any, beneficiary: Beneficiary, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        cast(forms.ModelChoiceField, self.fields["user"]).queryset = curators().exclude(
            pk=beneficiary.curator_id
        )
