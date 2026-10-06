from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, cast

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core.dates import is_minor
from apps.core.forms import DateInput, StyledFormMixin, TimeInput
from apps.core.models import (
    District,
    LeavingReason,
    Program,
    Skill,
    TrainingType,
    VolunteerSource,
)
from apps.core.normalize import normalize_phone

from .models import Volunteer, VolunteerStatus, Weekday
from .services import AvailabilitySlot


class VolunteerForm(StyledFormMixin, forms.ModelForm):
    """Карточка волонтёра (ФТ-В-01, ФТ-В-08). Сохраняет не форма, а сервис."""

    photo_upload = forms.FileField(
        label="Фото",
        required=False,
        help_text="JPEG, PNG или WEBP, до 20 МБ.",
        widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}),
    )
    remove_photo = forms.BooleanField(label="Удалить текущее фото", required=False)
    confirm_duplicates = forms.BooleanField(
        label="Это другой человек — всё равно сохранить", required=False
    )

    class Meta:
        model = Volunteer
        fields = (
            "last_name",
            "first_name",
            "middle_name",
            "birth_date",
            "phone",
            "email",
            "telegram_username",
            "city",
            "district",
            "skills",
            "programs",
            "source",
            "guardian_name",
            "guardian_relation",
            "guardian_phone",
            "guardian_email",
        )
        widgets = {
            "birth_date": DateInput(),
            "phone": forms.TextInput(attrs={"type": "tel", "autocomplete": "off"}),
            "guardian_phone": forms.TextInput(attrs={"type": "tel", "autocomplete": "off"}),
            "telegram_username": forms.TextInput(attrs={"autocapitalize": "none"}),
            "skills": forms.CheckboxSelectMultiple,
            "programs": forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # В списках — только действующие записи справочников
        references = {
            "district": District.objects.active(),
            "skills": Skill.objects.active(),
            "programs": Program.objects.active(),
            "source": VolunteerSource.objects.active(),
        }
        for name, queryset in references.items():
            cast(forms.ModelChoiceField, self.fields[name]).queryset = queryset
        for name in ("email", "guardian_email"):
            self.fields[name].widget.attrs.setdefault("autocomplete", "off")

    def clean_phone(self) -> str:
        return normalize_phone(self.cleaned_data["phone"])

    def clean_guardian_phone(self) -> str:
        return normalize_phone(self.cleaned_data.get("guardian_phone", ""))

    def clean_telegram_username(self) -> str:
        return (self.cleaned_data.get("telegram_username") or "").strip().lstrip("@")

    def clean_birth_date(self) -> datetime.date:
        birth_date: datetime.date = self.cleaned_data["birth_date"]
        if birth_date > timezone.localdate():
            raise ValidationError("Дата рождения не может быть в будущем.")
        return birth_date

    def clean(self) -> dict[str, Any]:
        super().clean()
        data = self.cleaned_data
        if is_minor(data.get("birth_date")):
            # ФТ-В-08: для несовершеннолетних данные представителя обязательны
            if not data.get("guardian_name"):
                self.add_error(
                    "guardian_name", "Волонтёру нет 18 лет — укажите законного представителя."
                )
            if not data.get("guardian_phone"):
                self.add_error("guardian_phone", "Укажите телефон представителя.")
        return data

    def service_data(self) -> dict[str, Any]:
        """Поля карточки для сервиса (без M2M и файлов)."""
        skip = {"skills", "programs"}
        return {key: self.cleaned_data[key] for key in self.Meta.fields if key not in skip}


class AvailabilityForm(StyledFormMixin, forms.Form):
    weekday = forms.TypedChoiceField(
        label="День", choices=[("", "—"), *Weekday.choices], coerce=int, required=False
    )
    time_from = forms.TimeField(label="с", required=False, widget=TimeInput())
    time_to = forms.TimeField(label="до", required=False, widget=TimeInput())

    def clean(self) -> dict[str, Any]:
        super().clean()
        data = self.cleaned_data
        values = [data.get("weekday"), data.get("time_from"), data.get("time_to")]
        filled = [v not in (None, "") for v in values]
        if any(filled) and not all(filled):
            raise ValidationError("Заполните день и время «с» и «до» или очистите строку.")
        if all(filled) and data["time_to"] <= data["time_from"]:
            raise ValidationError("Время «до» должно быть позже времени «с».")
        return data


class BaseAvailabilityFormSet(forms.BaseFormSet):
    def slots(self) -> list[AvailabilitySlot]:
        result = []
        for form in self.forms:
            data = form.cleaned_data
            if data and data.get("weekday") not in (None, ""):
                result.append((data["weekday"], data["time_from"], data["time_to"]))
        return sorted(set(result))


_AvailabilityFormSet = forms.formset_factory(
    AvailabilityForm, formset=BaseAvailabilityFormSet, extra=2, max_num=21, validate_max=True
)


def availability_formset(*args: Any, **kwargs: Any) -> BaseAvailabilityFormSet:
    """Строки доступности (prefix «slots»): пустые строки игнорируются."""
    kwargs.setdefault("prefix", "slots")
    return cast(BaseAvailabilityFormSet, _AvailabilityFormSet(*args, **kwargs))


def availability_initial(volunteer: Volunteer) -> list[dict[str, Any]]:
    return [
        {"weekday": slot.weekday, "time_from": slot.time_from, "time_to": slot.time_to}
        for slot in volunteer.availability.all()
    ]


class StatusChangeForm(StyledFormMixin, forms.Form):
    to_status = forms.ChoiceField(label="Новый статус")
    reason = forms.ModelChoiceField(
        label="Причина выбытия",
        queryset=LeavingReason.objects.active(),
        required=False,
        help_text="Обязательна для статуса «Выбыл».",
    )
    comment = forms.CharField(label="Комментарий", max_length=500, required=False)

    def __init__(self, *args: Any, transitions: list[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        cast(forms.ChoiceField, self.fields["to_status"]).choices = [
            (status, VolunteerStatus(status).label) for status in transitions
        ]

    def clean(self) -> dict[str, Any]:
        super().clean()
        data = self.cleaned_data
        if data.get("to_status") == VolunteerStatus.LEFT and not data.get("reason"):
            self.add_error("reason", "Укажите причину выбытия.")
        return data


class TrainingForm(StyledFormMixin, forms.Form):
    training_type = forms.ModelChoiceField(
        label="Обучение или инструктаж", queryset=TrainingType.objects.active()
    )
    completed_on = forms.DateField(label="Дата прохождения", widget=DateInput())
    conducted_by = forms.CharField(label="Кто провёл", max_length=200, required=False)


class HoursForm(StyledFormMixin, forms.Form):
    worked_on = forms.DateField(label="Дата", widget=DateInput())
    hours = forms.DecimalField(
        label="Часы",
        min_value=Decimal("0.25"),
        max_value=Decimal(24),
        max_digits=5,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.25", "inputmode": "decimal"}),
    )
    program = forms.ModelChoiceField(
        label="Программа", queryset=Program.objects.active(), required=False
    )
    comment = forms.CharField(
        label="Комментарий",
        max_length=500,
        help_text="Обязателен: за что начислены часы.",
    )
