from typing import Any

from django import forms


class DateInput(forms.DateInput):
    """Нативный выбор даты: браузер показывает ДД.ММ.ГГГГ, на сервер уходит ГГГГ-ММ-ДД."""

    input_type = "date"

    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        super().__init__(attrs, format="%Y-%m-%d")


class TimeInput(forms.TimeInput):
    input_type = "time"

    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        super().__init__(attrs, format="%H:%M")


class StyledFormMixin:
    """Добавляет классы Tailwind к виджетам формы — шаблоны остаются простыми."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():  # type: ignore[attr-defined]
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput | forms.CheckboxSelectMultiple):
                css = "h-4 w-4 rounded border-slate-300 text-brand-600"
            elif isinstance(widget, forms.RadioSelect):
                css = "h-4 w-4 border-slate-300 text-brand-600"
            elif isinstance(widget, forms.ClearableFileInput | forms.FileInput):
                css = (
                    "block w-full text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 "
                    "file:bg-slate-100 file:px-3 file:py-2 file:text-sm file:font-medium "
                    "hover:file:bg-slate-200"
                )
            else:
                css = "form-input"
            if css:
                widget.attrs["class"] = f"{widget.attrs.get('class', '')} {css}".strip()
            if field.help_text:
                widget.attrs.setdefault("aria-describedby", f"id_{name}_help")
