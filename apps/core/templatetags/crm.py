from typing import Any

from django import template
from django.http import HttpRequest

from apps.core.normalize import format_phone

register = template.Library()


@register.simple_tag
def query_replace(request: HttpRequest, **params: Any) -> str:
    """Текущая строка запроса с заменёнными параметрами: ссылки сортировки и страниц.

    {% query_replace request page=2 %} → «?status=active&page=2». Пустое значение удаляет
    параметр. Смена сортировки или фильтра сбрасывает номер страницы.
    """
    query = request.GET.copy()
    if "page" not in params:
        query.pop("page", None)
    for key, value in params.items():
        if value in (None, ""):
            query.pop(key, None)
        else:
            query[key] = str(value)
    encoded = query.urlencode()
    return f"?{encoded}" if encoded else "?"


@register.filter
def phone(value: str) -> str:
    return format_phone(value)


@register.filter
def get_item(mapping: dict[Any, Any], key: Any) -> Any:
    return mapping.get(key) if mapping else None


@register.filter
def ru_plural(number: int, forms: str) -> str:
    """Склонение после числа: {{ age|ru_plural:"год,года,лет" }} → «21 год», «24 года»."""
    one, few, many = forms.split(",")
    n = abs(int(number))
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{number} {word}"
