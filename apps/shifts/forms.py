from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, cast

from django import forms
from django.utils import timezone

from apps.core.forms import DateInput, StyledFormMixin, TimeInput
from apps.core.models import District, Program, ShiftType, Skill
from apps.interactions.forms import DateTimeInput
from apps.volunteers.models import Volunteer, VolunteerStatus

from .models import HOURS_STEP, MAX_HOURS, Assignment, AssignmentStatus, Shift
from .services import MAX_REPEAT_WEEKS, SHIFT_FIELDS


class HoursField(forms.DecimalField):
    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("label", "Часы")
        super().__init__(
            min_value=HOURS_STEP,
            max_value=MAX_HOURS,
            max_digits=5,
            decimal_places=2,
            widget=forms.NumberInput(attrs={"step": "0.25", "inputmode": "decimal"}),
            **kwargs,
        )


class ShiftForm(StyledFormMixin, forms.ModelForm):
    """ФТ-С-01: название, тип, дата и время, место, число волонтёров, навыки, программа."""

    class Meta:
        model = Shift
        fields = [*SHIFT_FIELDS, "skills"]
        widgets = {
            "starts_at": DateTimeInput(),
            "ends_at": DateTimeInput(),
            "description": forms.Textarea(attrs={"rows": 3}),
            "skills": forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        references: dict[str, Any] = {
            "shift_type": ShiftType.objects.active(),
            "district": District.objects.active(),
            "program": Program.objects.active(),
            "skills": Skill.objects.active(),
        }
        for name, queryset in references.items():
            cast(forms.ModelChoiceField, self.fields[name]).queryset = queryset
        self.fields["skills"].required = False
        instance = self.instance
        if not instance.pk:
            start = timezone.localtime().replace(minute=0, second=0, microsecond=0)
            start += datetime.timedelta(days=1)
            self.initial.setdefault("starts_at", start.replace(hour=10))
            self.initial.setdefault("ends_at", start.replace(hour=14))
            self.initial.setdefault("volunteers_needed", 1)

    def service_data(self) -> dict[str, Any]:
        return {key: self.cleaned_data[key] for key in SHIFT_FIELDS}


class ShiftCancelForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(
        label="Причина отмены",
        max_length=500,
        help_text="Волонтёры получат сообщение об отмене (без причины).",
    )


class AssignmentStatusForm(StyledFormMixin, forms.Form):
    to_status = forms.ChoiceField(label="Новый статус", widget=forms.RadioSelect)
    hours = HoursField(
        required=False,
        help_text="Только для «Пришёл». Пусто — по длительности смены или визита.",
    )
    comment = forms.CharField(label="Комментарий", max_length=500, required=False)

    def __init__(self, *args: Any, transitions: list[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        cast(forms.ChoiceField, self.fields["to_status"]).choices = [
            (value, AssignmentStatus(value).label) for value in transitions
        ]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if data.get("hours") is not None and data.get("to_status") != AssignmentStatus.ATTENDED:
            self.add_error("hours", "Часы вносятся только для статуса «Пришёл».")
        return data


class HoursForm(StyledFormMixin, forms.Form):
    hours = HoursField()


class AttendanceForm(forms.Form):
    """Лист явки после смены: по строке на каждого записанного (ФТ-С-04)."""

    CHOICES = [("", "Не отмечено"), ("yes", "Пришёл"), ("no", "Не пришёл")]

    def __init__(self, *args: Any, assignments: list[Assignment], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.assignments = assignments
        for a in assignments:
            initial = {
                AssignmentStatus.ATTENDED: "yes",
                AssignmentStatus.NO_SHOW: "no",
            }.get(AssignmentStatus(a.status), "")
            self.fields[f"mark_{a.pk}"] = forms.ChoiceField(
                label=a.volunteer.full_name,
                choices=self.CHOICES,
                required=False,
                initial=initial,
                widget=forms.RadioSelect(attrs={"class": "h-4 w-4 border-slate-300"}),
            )
            hours = HoursField(required=False, initial=a.hours or a.default_hours)
            hours.widget.attrs["class"] = "form-input w-24"
            hours.widget.attrs["aria-label"] = f"Часы: {a.volunteer.full_name}"
            self.fields[f"hours_{a.pk}"] = hours

    def rows(self) -> list[tuple[Assignment, Any, Any]]:
        return [(a, self[f"mark_{a.pk}"], self[f"hours_{a.pk}"]) for a in self.assignments]

    def marks(self) -> list[tuple[Assignment, bool, Decimal | None]]:
        result = []
        for a in self.assignments:
            mark = self.cleaned_data.get(f"mark_{a.pk}")
            if mark in ("yes", "no"):
                result.append((a, mark == "yes", self.cleaned_data.get(f"hours_{a.pk}")))
        return result


class PickFilterForm(StyledFormMixin, forms.Form):
    """Подбор волонтёров на смену (ФТ-С-03)."""

    q = forms.CharField(label="ФИО", required=False)
    skills = forms.BooleanField(label="Есть все требуемые навыки", required=False)
    district = forms.BooleanField(label="Из района смены", required=False)
    available = forms.BooleanField(label="Доступен в это время", required=False)


def active_volunteers(current: Volunteer | None = None) -> Any:
    queryset = Volunteer.objects.filter(status=VolunteerStatus.ACTIVE, is_archived=False)
    if current is not None:
        queryset = queryset | Volunteer.objects.filter(pk=current.pk)
    return queryset.only("id", "last_name", "first_name", "middle_name").order_by(
        "last_name", "first_name"
    )


class VisitForm(StyledFormMixin, forms.Form):
    """ФТ-С-05: визит волонтёра к подопечному; регулярные визиты — повтор по неделям."""

    volunteer = forms.ModelChoiceField(label="Волонтёр", queryset=Volunteer.objects.none())
    date = forms.DateField(label="Дата", widget=DateInput())
    time_from = forms.TimeField(label="С", widget=TimeInput())
    time_to = forms.TimeField(label="До", widget=TimeInput())
    task_comment = forms.CharField(
        label="Комментарий к заданию",
        max_length=1000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Волонтёр увидит его в боте. Не пишите о здоровье и документах.",
    )
    program = forms.ModelChoiceField(
        label="Программа", queryset=Program.objects.none(), required=False
    )
    repeat_weeks = forms.IntegerField(
        label="Повторять ещё недель",
        min_value=0,
        max_value=MAX_REPEAT_WEEKS,
        initial=0,
        help_text=f"Регулярные визиты: в тот же день недели, до {MAX_REPEAT_WEEKS} недель.",
    )

    def __init__(self, *args: Any, edit: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        cast(forms.ModelChoiceField, self.fields["volunteer"]).queryset = active_volunteers()
        cast(forms.ModelChoiceField, self.fields["program"]).queryset = Program.objects.active()
        if edit:
            del self.fields["volunteer"]
            del self.fields["repeat_weeks"]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        day, time_from, time_to = data.get("date"), data.get("time_from"), data.get("time_to")
        if day and time_from and time_to:
            tz = timezone.get_current_timezone()
            data["starts_at"] = datetime.datetime.combine(day, time_from, tzinfo=tz)
            data["ends_at"] = datetime.datetime.combine(day, time_to, tzinfo=tz)
            if time_to <= time_from:
                self.add_error("time_to", "Окончание визита должно быть позже начала.")
        return data
