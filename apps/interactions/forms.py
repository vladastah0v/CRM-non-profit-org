from __future__ import annotations

from typing import Any, cast

from django import forms
from django.utils import timezone

from apps.accounts.models import User
from apps.core.forms import DateInput, StyledFormMixin

from .models import InteractionKind


class DateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        super().__init__(attrs, format="%Y-%m-%dT%H:%M")


class InteractionForm(StyledFormMixin, forms.Form):
    """ФТ-ВЗ-01: тип, дата, текст. Автор — тот, кто сохраняет запись."""

    kind = forms.ChoiceField(label="Тип", choices=InteractionKind.choices)
    happened_at = forms.DateTimeField(label="Когда", widget=DateTimeInput())
    text = forms.CharField(
        label="Содержание",
        max_length=5000,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Только то, что нужно для работы. Не записывайте сюда диагнозы и документы.",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.initial.setdefault(
            "happened_at", timezone.localtime().replace(second=0, microsecond=0)
        )


class TaskForm(StyledFormMixin, forms.Form):
    """ФТ-ВЗ-02: задача-напоминание с датой и ответственным."""

    text = forms.CharField(label="Что сделать", max_length=500)
    due_date = forms.DateField(label="Срок", widget=DateInput())
    assignee = forms.ModelChoiceField(label="Ответственный", queryset=User.objects.none())

    def __init__(self, *args: Any, assignees: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        cast(forms.ModelChoiceField, self.fields["assignee"]).queryset = assignees
