"""Глобальный поиск по ФИО и телефону во всех доступных пользователю карточках (п. 3.10).

Поиск идёт через выборки списков модулей — с их проверкой прав (visible_to) и набором
полей «О» для ограниченного доступа. Текст запроса никуда не сохраняется.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.beneficiaries import permissions as beneficiary_permissions
from apps.beneficiaries.selectors import beneficiary_list
from apps.volunteers import permissions as volunteer_permissions
from apps.volunteers.selectors import volunteer_list

MIN_QUERY = 2
LIMIT = 20


@dataclass
class Group:
    title: str
    items: list[Any]
    total: int
    list_url: str


def _params(query: str) -> QueryDict:
    params = QueryDict(mutable=True)
    params["q"] = query
    return params


def search_cards(user: Any, query: str) -> list[Group]:
    groups: list[Group] = []
    if volunteer_permissions.can_view(user):
        found = volunteer_list(user, _params(query)).qs
        groups.append(
            Group("Волонтёры", list(found[:LIMIT]), found.count(), reverse("volunteers:list"))
        )
    if beneficiary_permissions.can_view_list(user):
        found = beneficiary_list(user, _params(query)).qs
        groups.append(
            Group("Подопечные", list(found[:LIMIT]), found.count(), reverse("beneficiaries:list"))
        )
    return groups


@login_required
@require_GET
@never_cache
def search(request: HttpRequest) -> HttpResponse:
    query = " ".join(request.GET.get("q", "").split())[:100]
    groups = search_cards(request.user, query) if len(query) >= MIN_QUERY else []
    can_search = volunteer_permissions.can_view(request.user) or (
        beneficiary_permissions.can_view_list(request.user)
    )
    context = {
        "query": query,
        "groups": groups,
        "too_short": 0 < len(query) < MIN_QUERY,
        "can_search": can_search,
        "found": sum(g.total for g in groups),
    }
    return render(request, "core/search.html", context)
