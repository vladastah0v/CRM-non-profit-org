"""Сохранение фильтров списков в профиле пользователя (п. 3.10, ФТ-В-05).

Сценарий: пользователь настроил фильтры → они сохраняются; открыл список заново (в том
числе после выхода) → фильтры восстанавливаются; «Сбросить» → сохранённое удаляется.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.http import HttpRequest, QueryDict

from .models import SavedListFilter

RESET_PARAM = "reset"


def _clean(params: QueryDict, keys: Iterable[str]) -> str:
    """Только разрешённые непустые параметры — без поиска и номера страницы."""
    allowed = set(keys)
    clean = QueryDict(mutable=True)
    for key in params:
        if key in allowed:
            values = [v for v in params.getlist(key) if v.strip()]
            if values:
                clean.setlist(key, values)
    return clean.urlencode()


def restore_or_save(request: HttpRequest, list_key: str, keys: Iterable[str]) -> str | None:
    """Вернуть строку запроса для перенаправления, если нужно восстановить фильтры.

    - ``?reset=1`` — удалить сохранённые фильтры;
    - есть параметры — сохранить разрешённые из них;
    - параметров нет — вернуть сохранённые (view перенаправит на них).
    """
    user: Any = request.user
    saved = SavedListFilter.objects.filter(user=user, list_key=list_key)
    if request.GET.get(RESET_PARAM):
        saved.delete()
        return None
    if request.GET:
        query = _clean(request.GET, keys)
        if query:
            SavedListFilter.objects.update_or_create(
                user=user, list_key=list_key, defaults={"query": query}
            )
        else:
            saved.delete()
        return None
    stored = saved.values_list("query", flat=True).first()
    return stored or None
