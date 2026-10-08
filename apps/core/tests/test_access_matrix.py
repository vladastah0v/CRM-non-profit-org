"""Систематическая проверка прав (раздел 9.1 ТЗ «Тесты прав доступа»; матрица 2.2, НФТ-ИБ-08).

Для каждого адреса приложения — кому он доступен. Все объекты принадлежат «чужому»
куратору (owner), второй куратор (curator) его подопечного видеть не должен ни по прямой
ссылке, ни в действиях. Запрещённый ответ — 403 или 404 (404 не раскрывает, что объект
существует). POST-адреса проверяются только для тех, кому запрещено: разрешённый POST
изменил бы данные.

Тест test_every_route_is_covered не даёт добавить адрес без строки в таблице.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any

import pytest
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone

from apps.beneficiaries import services as beneficiary_services
from apps.beneficiaries.models import CuratorAccess
from apps.beneficiaries.tests.conftest import (  # noqa: F401
    brefs,
    curator,
    give_consent,
    make_beneficiary,
    to_in_work,
)
from apps.consents.models import Consent
from apps.core.models import AssistanceType, ShiftType, VolunteerDocumentType
from apps.imports.models import ImportJob
from apps.interactions.models import Interaction, Task
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from apps.requests import services as request_services
from apps.requests.tests.conftest import assistance_data
from apps.shifts import services as shift_services
from apps.volunteers import services as volunteer_services
from apps.volunteers.tests.conftest import (  # noqa: F401
    make_ready_for_activation,
    make_volunteer,
    refs,
)

pytestmark = pytest.mark.django_db

ADMIN, COORD, OWNER, CURATOR, ANALYST = "admin", "coordinator", "owner", "curator", "analyst"
ROLES = (ADMIN, COORD, OWNER, CURATOR, ANALYST)
EVERYONE = frozenset(ROLES)
CARD_STAFF = frozenset({ADMIN, COORD, OWNER, CURATOR})
VOLUNTEER_MANAGERS = frozenset({ADMIN, COORD})
CASE_MANAGERS = frozenset({ADMIN, OWNER})
ONLY_ADMIN = frozenset({ADMIN})
NOBODY: frozenset[str] = frozenset()


@pytest.fixture
def world(make_user, make_volunteer, make_beneficiary, refs, brefs) -> dict[str, Any]:  # noqa: F811
    users = {
        ADMIN: make_user("admin"),
        COORD: make_user("coordinator"),
        OWNER: make_user("curator"),
        CURATOR: make_user("curator"),
        ANALYST: make_user("analyst"),
    }
    volunteer = make_ready_for_activation(make_volunteer(), refs)
    volunteer_services.change_status(volunteer, "active")
    volunteer_document = volunteer_services.add_document(
        volunteer, document_type=VolunteerDocumentType.objects.get(name="Личная медицинская книжка")
    )
    beneficiary = to_in_work(make_beneficiary(curator=users[OWNER]))
    beneficiary_services.save_sensitive(beneficiary, {"passport": "4500 123456"})
    person = beneficiary_services.add_related_person(
        beneficiary, {"full_name": "Смирнова Ольга", "relation": "дочь"}
    )
    access = CuratorAccess.objects.create(beneficiary=beneficiary, user=users[ADMIN])
    food = AssistanceType.objects.create(name="Продукты", unit="шт.")
    help_request = request_services.create_request(
        beneficiary, {"assistance_type": food, "description": "Продукты"}
    )
    assistance = request_services.register_assistance(
        beneficiary, assistance_data(food, users[OWNER]), help_request=help_request
    )
    interaction = Interaction.objects.create(
        beneficiary=beneficiary,
        kind="call",
        happened_at=timezone.now(),
        text="Звонок",
        created_by=users[OWNER],
    )
    task = Task.objects.create(
        beneficiary=beneficiary,
        text="Позвонить",
        due_date=timezone.localdate(),
        assignee=users[OWNER],
        created_by=users[OWNER],
    )
    starts = timezone.now() + datetime.timedelta(days=1)
    shift = shift_services.create_shift(
        {
            "title": "Смена",
            "shift_type": ShiftType.objects.create(name="Раздача"),
            "starts_at": starts,
            "ends_at": starts + datetime.timedelta(hours=3),
            "address": "Склад",
            "volunteers_needed": 2,
        }
    )
    assignment = shift_services.assign_to_shift(shift, volunteer)
    second_volunteer = make_ready_for_activation(make_volunteer(), refs)
    volunteer_services.change_status(second_volunteer, "active")
    (visit,) = shift_services.create_visits(
        beneficiary,
        second_volunteer,
        starts_at=starts,
        ends_at=starts + datetime.timedelta(hours=1),
    )
    (notification,) = notify([users[OWNER]], NotificationKind.TASK_DUE, "Срок", url="/")
    job = ImportJob.objects.create(kind="volunteers", file_format="xlsx", created_by=users[ADMIN])
    return {
        "users": users,
        "volunteer": volunteer,
        "volunteer_consent": Consent.objects.filter(volunteer=volunteer).first(),
        "volunteer_document": volunteer_document,
        "beneficiary": beneficiary,
        "beneficiary_consent": Consent.objects.filter(beneficiary=beneficiary).first(),
        "person": person,
        "access": access,
        "request": help_request,
        "assistance": assistance,
        "interaction": interaction,
        "task": task,
        "shift": shift,
        "assignment": assignment,
        "visit": visit,
        "notification": notification,
        "job": job,
    }


Args = Callable[[dict[str, Any]], list[Any]]


def pk(key: str) -> Args:
    return lambda w: [w[key].pk]


def none(w: dict[str, Any]) -> list[Any]:
    return []


V, B = pk("volunteer"), pk("beneficiary")

# (имя адреса, аргументы, метод, кому разрешено)
TABLE: list[tuple[str, Args, str, frozenset[str]]] = [
    ("core:home", none, "GET", EVERYONE),
    ("core:search", none, "GET", EVERYONE),
    ("accounts:password_change", none, "GET", EVERYONE),
    # Волонтёры: П — координатор и админ, Ч (О) — кураторы, аналитик — нет
    ("volunteers:list", none, "GET", CARD_STAFF),
    ("volunteers:create", none, "GET", VOLUNTEER_MANAGERS),
    ("volunteers:detail", V, "GET", CARD_STAFF),
    ("volunteers:edit", V, "GET", VOLUNTEER_MANAGERS),
    ("volunteers:status", V, "GET", VOLUNTEER_MANAGERS),
    ("volunteers:training_add", V, "GET", VOLUNTEER_MANAGERS),
    ("volunteers:document_add", V, "GET", VOLUNTEER_MANAGERS),
    (
        "volunteers:document_archive",
        lambda w: [w["volunteer"].pk, w["volunteer_document"].pk],
        "POST",
        VOLUNTEER_MANAGERS,
    ),
    ("volunteers:hours_add", V, "GET", VOLUNTEER_MANAGERS),
    ("volunteers:consent_add", V, "GET", VOLUNTEER_MANAGERS),
    (
        "volunteers:consent_revoke",
        lambda w: [w["volunteer"].pk, w["volunteer_consent"].pk],
        "GET",
        VOLUNTEER_MANAGERS,
    ),
    ("volunteers:duplicate_resolve", V, "POST", VOLUNTEER_MANAGERS),
    ("volunteers:bot_code", V, "POST", VOLUNTEER_MANAGERS),
    ("volunteers:bot_unlink", V, "POST", VOLUNTEER_MANAGERS),
    ("volunteers:archive", V, "POST", VOLUNTEER_MANAGERS),
    ("volunteers:restore", V, "POST", VOLUNTEER_MANAGERS),
    # Подопечные: свой куратор и админ — П; координатор — Ч (О); чужой куратор — ничего
    ("beneficiaries:list", none, "GET", CARD_STAFF),
    ("beneficiaries:create", none, "GET", frozenset({ADMIN, OWNER, CURATOR})),
    ("beneficiaries:detail", B, "GET", frozenset({ADMIN, COORD, OWNER})),
    ("beneficiaries:edit", B, "GET", CASE_MANAGERS),
    ("beneficiaries:status", B, "GET", CASE_MANAGERS),
    ("beneficiaries:sensitive", B, "GET", CASE_MANAGERS),
    ("beneficiaries:sensitive_edit", B, "GET", CASE_MANAGERS),
    ("beneficiaries:document_add", B, "GET", CASE_MANAGERS),
    ("beneficiaries:related_add", B, "GET", CASE_MANAGERS),
    (
        "beneficiaries:related_edit",
        lambda w: [w["beneficiary"].pk, w["person"].pk],
        "GET",
        CASE_MANAGERS,
    ),
    (
        "beneficiaries:related_archive",
        lambda w: [w["beneficiary"].pk, w["person"].pk],
        "POST",
        CASE_MANAGERS,
    ),
    ("beneficiaries:consent_add", B, "GET", CASE_MANAGERS),
    (
        "beneficiaries:consent_revoke",
        lambda w: [w["beneficiary"].pk, w["beneficiary_consent"].pk],
        "GET",
        CASE_MANAGERS,
    ),
    ("beneficiaries:curators", B, "GET", ONLY_ADMIN),
    (
        "beneficiaries:access_revoke",
        lambda w: [w["beneficiary"].pk, w["access"].pk],
        "POST",
        ONLY_ADMIN,
    ),
    ("beneficiaries:duplicate_resolve", B, "POST", ONLY_ADMIN),
    ("beneficiaries:archive", B, "POST", CASE_MANAGERS),
    ("beneficiaries:restore", B, "POST", CASE_MANAGERS),
    # Заявки и помощь: координатор — Ч, свой куратор — С
    ("requests:list", none, "GET", CARD_STAFF),
    ("requests:create", B, "GET", CASE_MANAGERS),
    ("requests:detail", pk("request"), "GET", frozenset({ADMIN, COORD, OWNER})),
    ("requests:edit", pk("request"), "GET", CASE_MANAGERS),
    ("requests:status", pk("request"), "GET", CASE_MANAGERS),
    ("requests:archive", pk("request"), "POST", ONLY_ADMIN),
    ("requests:assistance_add", B, "GET", CASE_MANAGERS),
    ("requests:assistance_edit", pk("assistance"), "GET", CASE_MANAGERS),
    ("requests:assistance_archive", pk("assistance"), "POST", CASE_MANAGERS),
    # Взаимодействия и задачи: пишет тот, кто работает с карточкой; правит автор
    ("interactions:add", lambda w: ["beneficiary", w["beneficiary"].pk], "GET", CASE_MANAGERS),
    ("interactions:edit", pk("interaction"), "GET", CASE_MANAGERS),
    ("interactions:archive", pk("interaction"), "POST", CASE_MANAGERS),
    ("interactions:tasks", none, "GET", CARD_STAFF),
    (
        "interactions:task_add",
        lambda w: ["volunteer", w["volunteer"].pk],
        "GET",
        VOLUNTEER_MANAGERS,
    ),
    ("interactions:task_edit", pk("task"), "GET", CASE_MANAGERS),
    ("interactions:task_close", pk("task"), "POST", CASE_MANAGERS),
    # Смены: координатор и админ — П, кураторы — Ч, визиты — только свой куратор
    ("shifts:list", none, "GET", CARD_STAFF),
    ("shifts:create", none, "GET", VOLUNTEER_MANAGERS),
    ("shifts:detail", pk("shift"), "GET", CARD_STAFF),
    ("shifts:edit", pk("shift"), "GET", VOLUNTEER_MANAGERS),
    ("shifts:cancel", pk("shift"), "GET", VOLUNTEER_MANAGERS),
    ("shifts:archive", pk("shift"), "POST", VOLUNTEER_MANAGERS),
    ("shifts:pick", pk("shift"), "GET", VOLUNTEER_MANAGERS),
    ("shifts:attendance", pk("shift"), "GET", VOLUNTEER_MANAGERS),
    ("shifts:assignment_status", pk("assignment"), "GET", VOLUNTEER_MANAGERS),
    ("shifts:assignment_hours", pk("assignment"), "GET", VOLUNTEER_MANAGERS),
    ("shifts:visit_create", B, "GET", VOLUNTEER_MANAGERS),
    ("shifts:visit_detail", pk("visit"), "GET", frozenset({ADMIN, COORD, OWNER})),
    ("shifts:visit_edit", pk("visit"), "GET", VOLUNTEER_MANAGERS),
    # Отчёты: аналитик — агрегаты; координатор — по волонтёрам; куратор — по своим
    ("reports:index", none, "GET", EVERYONE),
    ("reports:program", none, "GET", frozenset({ADMIN, OWNER, CURATOR, ANALYST})),
    ("reports:volunteers", none, "GET", frozenset({ADMIN, COORD, ANALYST})),
    ("reports:funnel", none, "GET", frozenset({ADMIN, COORD, ANALYST})),
    ("reports:export", lambda w: ["beneficiaries"], "GET", CARD_STAFF),
    # Администрирование
    ("imports:list", none, "GET", ONLY_ADMIN),
    ("imports:template", lambda w: ["volunteers"], "GET", ONLY_ADMIN),
    ("imports:detail", pk("job"), "GET", ONLY_ADMIN),
    ("imports:errors", pk("job"), "GET", ONLY_ADMIN),
    ("imports:confirm", pk("job"), "POST", ONLY_ADMIN),
    ("imports:cancel", pk("job"), "POST", ONLY_ADMIN),
    ("consents:erasure_list", none, "GET", ONLY_ADMIN),
    ("consents:subject_export", lambda w: ["beneficiary", w["beneficiary"].pk], "GET", ONLY_ADMIN),
    ("consents:anonymize", lambda w: ["beneficiary", w["beneficiary"].pk], "GET", ONLY_ADMIN),
    ("consents:dismiss_erasure", lambda w: ["volunteer", w["volunteer"].pk], "GET", ONLY_ADMIN),
    # Уведомления — только свои
    ("notifications:list", none, "GET", EVERYONE),
    ("notifications:open", pk("notification"), "POST", frozenset({OWNER})),
    ("notifications:read_all", none, "POST", EVERYONE),
    # API для сотрудников (документация) — сессия со 2FA
    ("api-v1:schema", none, "GET", EVERYONE),
    ("api-v1:docs", none, "GET", EVERYONE),
]

# API бота и приём анкет: сессия сотрудника не подходит ни для какой роли
SERVICE_API = [
    ("api-v1:bot-me", none),
    ("api-v1:bot-assignments", none),
    ("api-v1:bot-assignment", pk("assignment")),
    ("api-v1:bot-shifts", none),
]

# Адреса вне матрицы ролей: вход, публичные проверки, подписанные ссылки, API сервисов
NOT_ROLE_BASED = {
    "core:healthz",
    "accounts:login",
    "accounts:otp_verify",
    "accounts:otp_setup",
    "accounts:logout",
    "consents:download",  # подписанная ссылка, привязанная к пользователю (test_files)
    "api-v1:intake-volunteer",  # подпись HMAC (test_intake)
    "api-v1:intake-beneficiary",
    "api-v1:bot-link",  # сервисный токен (shifts/tests/test_api)
    "api-v1:bot-unlink",
    "api-v1:bot-confirm",
    "api-v1:bot-decline",
    "api-v1:bot-attendance",
    "api-v1:bot-signup",
}


def _route_names(patterns: list[Any], namespace: str = "") -> set[str]:
    names: set[str] = set()
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            ns = pattern.namespace or ""
            full = f"{namespace}:{ns}" if namespace and ns else (ns or namespace)
            names |= _route_names(pattern.url_patterns, full)
        elif isinstance(pattern, URLPattern) and pattern.name:
            names.add(f"{namespace}:{pattern.name}" if namespace else pattern.name)
    return names


def test_every_route_is_covered():
    routes = {
        name for name in _route_names(get_resolver().url_patterns) if not name.startswith("admin:")
    }
    covered = {name for name, *_ in TABLE} | {name for name, _ in SERVICE_API} | NOT_ROLE_BASED
    assert routes - covered == set(), "Адрес без правил доступа в TABLE"


def _is_allowed(response: Any) -> bool:
    if response.status_code == 200:
        return True
    if response.status_code == 302:
        return "/accounts/login" not in response["Location"]
    return False


def test_role_matrix(world, verified_client):
    clients = {role: verified_client(user) for role, user in world["users"].items()}
    problems = []
    for name, args, method, allowed in TABLE:
        url = reverse(name, args=args(world))
        for role in ROLES:
            if method == "POST" and role in allowed:
                continue  # разрешённый POST изменил бы данные — его проверяют тесты модулей
            client = clients[role]
            response = client.get(url) if method == "GET" else client.post(url)
            if role in allowed and not _is_allowed(response):
                problems.append(f"{role} не может открыть {name}: {response.status_code}")
            if role not in allowed and response.status_code not in (403, 404):
                problems.append(f"{role} получил доступ к {name}: {response.status_code}")
    assert problems == []


def test_service_api_not_available_by_session(world, verified_client):
    for role, user in world["users"].items():
        client = verified_client(user)
        for name, args in SERVICE_API:
            response = client.get(reverse(name, args=args(world)), HTTP_X_TELEGRAM_USER_ID="1")
            assert response.status_code in (401, 403), (role, name)


def test_anonymous_redirected_to_login(world):
    client = Client()
    for name, args, method, _ in TABLE:
        url = reverse(name, args=args(world))
        response = client.get(url) if method == "GET" else client.post(url)
        if name.startswith("api-v1:"):
            assert response.status_code in (401, 403), name
        else:
            assert response.status_code in (302, 403), name
            if response.status_code == 302:
                assert "/accounts/login" in response["Location"], name
