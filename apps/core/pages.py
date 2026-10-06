"""Общие помощники экранов карточек (волонтёры, подопечные)."""

from __future__ import annotations

from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render


def apply_service_errors(form: forms.BaseForm, exc: ValidationError) -> None:
    """Ошибки сервиса — в форму: по полям, если такое поле есть, иначе общей ошибкой."""
    if hasattr(exc, "error_dict"):
        for field, errors in exc.error_dict.items():
            for error in errors:
                form.add_error(field if field in form.fields else None, error)
    else:
        form.add_error(None, exc)


def action_page(
    request: HttpRequest,
    form: forms.BaseForm,
    *,
    subject: Any,
    title: str,
    submit: str,
    **extra: Any,
) -> HttpResponse:
    """Страница одного действия с карточкой. `subject` — карточка с get_absolute_url,
    full_name и статусом (status_badge, get_status_display)."""
    context = {
        "form": form,
        "title": title,
        "submit": submit,
        "back_url": subject.get_absolute_url(),
        "subject_name": subject.full_name,
        "badge_class": getattr(subject, "status_badge", ""),
        "badge_text": subject.get_status_display(),
        **extra,
    }
    return render(request, "partials/action_page.html", context)
