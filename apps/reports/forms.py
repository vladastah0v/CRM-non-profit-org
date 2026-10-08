from __future__ import annotations

import datetime
from typing import Any

from django import forms

from apps.core.forms import DateInput, StyledFormMixin
from apps.core.models import Program

from .services import WITHOUT_PROGRAM, Period, previous_quarter

MAX_PERIOD = datetime.timedelta(days=366 * 5)


class PeriodForm(StyledFormMixin, forms.Form):
    """Период отчёта. По умолчанию — прошлый квартал (сценарий 9.2)."""

    date_from = forms.DateField(label="С", widget=DateInput())
    date_to = forms.DateField(label="По", widget=DateInput())

    def __init__(self, data: Any = None, **kwargs: Any) -> None:
        default = previous_quarter()
        if not data or "date_from" not in data:
            data = {
                "date_from": default.date_from.isoformat(),
                "date_to": default.date_to.isoformat(),
                **(dict(data.items()) if data else {}),
            }
        super().__init__(data, **kwargs)

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        start, end = data.get("date_from"), data.get("date_to")
        if start and end:
            if end < start:
                self.add_error("date_to", "Конец периода раньше начала.")
            elif end - start > MAX_PERIOD:
                self.add_error("date_to", "Период — не больше 5 лет.")
        return data

    @property
    def period(self) -> Period:
        return Period(self.cleaned_data["date_from"], self.cleaned_data["date_to"])


class ProgramReportForm(PeriodForm):
    program = forms.ChoiceField(label="Программа", required=False)

    def __init__(self, data: Any = None, **kwargs: Any) -> None:
        super().__init__(data, **kwargs)
        choices = [("", "Все программы"), (WITHOUT_PROGRAM, "Без программы")]
        choices += [(str(p.pk), str(p)) for p in Program.objects.all()]
        self.fields["program"].choices = choices  # type: ignore[attr-defined]

    @property
    def selected_program(self) -> Program | str | None:
        value = self.cleaned_data.get("program") or ""
        if not value:
            return None
        if value == WITHOUT_PROGRAM:
            return WITHOUT_PROGRAM
        return Program.objects.get(pk=value)
