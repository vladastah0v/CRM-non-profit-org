from typing import Any

from django import forms


class StyledFormMixin:
    """Добавляет классы Tailwind к виджетам формы — шаблоны остаются простыми."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():  # type: ignore[attr-defined]
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                css = "h-4 w-4 rounded border-slate-300 text-brand-600"
            elif isinstance(widget, forms.CheckboxSelectMultiple | forms.RadioSelect):
                css = ""
            else:
                css = "form-input"
            if css:
                widget.attrs["class"] = f"{widget.attrs.get('class', '')} {css}".strip()
            if field.help_text:
                widget.attrs.setdefault("aria-describedby", f"id_{name}_help")
