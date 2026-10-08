"""Данные расчётного объёма для нагрузочной проверки (п. 5.3 ТЗ, этап 9).

По умолчанию — объём на 3 года: 5 000 волонтёров, 20 000 подопечных, 500 000 фактов
помощи, плюс заявки, смены, сотрудники. Записи создаются пачками (bulk_create), минуя
сервисы: здесь важен объём, а не бизнес-правила. Все ФИО и телефоны вымышленные.

Работает только в отдельной базе с именем на «_load» (см. scripts/load/run.sh) —
чтобы случайно не наполнить рабочую или dev-базу. Выдаёт файл с сессиями сотрудников
для Locust (вход с 2FA уже пройден).
"""

from __future__ import annotations

import datetime
import json
import random
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.cached_db import SessionStore
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection, transaction
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY

from apps.accounts.models import Role, SecureTOTPDevice, User, UserRole
from apps.beneficiaries.models import Beneficiary, CaseStatus
from apps.core.models import (
    AssistanceType,
    BeneficiaryCategory,
    District,
    LeavingReason,
    Program,
    ShiftType,
    Skill,
)
from apps.core.normalize import normalize_name
from apps.requests.models import Assistance, HelpRequest, RequestStatus
from apps.shifts.models import Assignment, AssignmentKind, AssignmentStatus, Shift
from apps.volunteers.models import Volunteer, VolunteerStatus

LAST = ["Иванов", "Смирнов", "Кузнецов", "Попов", "Васильев", "Петров", "Соколов",
        "Михайлов", "Новиков", "Фёдоров", "Морозов", "Волков", "Алексеев", "Лебедев",
        "Семёнов", "Егоров", "Павлов", "Козлов", "Степанов", "Николаев"]  # fmt: skip
FIRST_M = ["Александр", "Сергей", "Дмитрий", "Андрей", "Алексей", "Иван", "Михаил", "Пётр"]
FIRST_F = ["Елена", "Ольга", "Наталья", "Мария", "Анна", "Татьяна", "Ирина", "Галина"]
MIDDLE_M = ["Иванович", "Петрович", "Сергеевич", "Алексеевич", "Николаевич"]
MIDDLE_F = ["Ивановна", "Петровна", "Сергеевна", "Алексеевна", "Николаевна"]
BATCH = 5000


def person(rng: random.Random) -> tuple[str, str, str]:
    last = rng.choice(LAST)
    if rng.random() < 0.6:
        return last + "а", rng.choice(FIRST_F), rng.choice(MIDDLE_F)
    return last, rng.choice(FIRST_M), rng.choice(MIDDLE_M)


class Command(BaseCommand):
    help = "Наполнить отдельную базу данными расчётного объёма для нагрузочной проверки."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--volunteers", type=int, default=5_000)
        parser.add_argument("--beneficiaries", type=int, default=20_000)
        parser.add_argument("--assistance", type=int, default=500_000)
        parser.add_argument("--curators", type=int, default=30)
        parser.add_argument("--sessions", type=Path, default=Path("tmp/load-sessions.json"))
        parser.add_argument("--seed", type=int, default=2026)

    def handle(self, *args: Any, **options: Any) -> None:
        name = str(connection.settings_dict["NAME"])
        if not name.endswith("_load"):
            raise CommandError(
                f"База «{name}» — не нагрузочная. Запускайте через scripts/load/run.sh "
                "(база с именем на _load)."
            )
        if Volunteer.objects.exists() or Beneficiary.objects.exists():
            raise CommandError("База уже наполнена — пересоздайте её (scripts/load/run.sh).")
        rng = random.Random(options["seed"])  # noqa: S311 — тестовые данные, не криптография
        started = time.monotonic()
        with transaction.atomic():
            refs = self._references()
            staff = self._staff(options["curators"])
            volunteers = self._volunteers(rng, options["volunteers"], refs)
            beneficiaries = self._beneficiaries(rng, options["beneficiaries"], refs, staff)
            self._assistance(rng, options["assistance"], beneficiaries, refs, staff)
            self._shifts(rng, volunteers, refs)
        sessions = self._sessions(staff)
        options["sessions"].write_text(json.dumps(sessions, ensure_ascii=False), encoding="utf-8")
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE")
        self.stdout.write(
            f"Готово за {time.monotonic() - started:.0f} с: волонтёров {len(volunteers)}, "
            f"подопечных {len(beneficiaries)}, помощи {Assistance.objects.count()}. "
            f"Сессии — {options['sessions']}"
        )

    # --- справочники и сотрудники ---
    def _references(self) -> dict[str, list[Any]]:
        def make(model: Any, names: list[str]) -> list[Any]:
            return [model.objects.get_or_create(name=n)[0] for n in names]

        return {
            "skills": make(Skill, ["Вождение", "Первая помощь", "Готовка", "Ремонт", "Педагогика"]),
            "districts": make(District, [f"Район {i}" for i in range(1, 13)]),
            "programs": make(Program, ["Тёплый дом", "Продукты", "Здоровье", "Дети", "Старшие"]),
            "categories": make(
                BeneficiaryCategory, ["Пожилые", "Семьи", "Инвалидность", "Бездомные"]
            ),
            "types": make(
                AssistanceType, ["Продуктовый набор", "Лекарства", "Одежда", "Денежная помощь"]
            ),
            "shift_types": make(ShiftType, ["Раздача", "Склад", "Мероприятие"]),
        }

    def _staff(self, curators: int) -> dict[str, list[User]]:
        roles = {r.code: r for r in Role.objects.all()}
        staff: dict[str, list[User]] = {}
        plan = {"admin": 2, "coordinator": 6, "curator": curators, "analyst": 2}
        for code, count in plan.items():
            users = []
            for i in range(count):
                user = User.objects.create_user(
                    username=f"load-{code}-{i}",
                    password=None,
                    last_name="Нагрузкин",
                    first_name=code,
                )
                UserRole.objects.create(user=user, role=roles[code])
                SecureTOTPDevice.objects.create(user=user, name="load", confirmed=True)
                users.append(user)
            staff[code] = users
        return staff

    # --- карточки ---
    def _volunteers(self, rng: random.Random, count: int, refs: dict[str, Any]) -> list[Volunteer]:
        statuses = [VolunteerStatus.ACTIVE] * 6 + [
            VolunteerStatus.APPLICATION, VolunteerStatus.PAUSED, VolunteerStatus.LEFT
        ]  # fmt: skip
        reason = LeavingReason.objects.get_or_create(name="Переезд")[0]
        today = timezone.localdate()
        rows = []
        for i in range(count):
            last, first, middle = person(rng)
            status = rng.choice(statuses)
            started = today - datetime.timedelta(days=rng.randrange(3 * 365))
            rows.append(
                Volunteer(
                    last_name=last,
                    first_name=first,
                    middle_name=middle,
                    birth_date=datetime.date(
                        1960 + rng.randrange(45), 1 + rng.randrange(12), 1 + rng.randrange(28)
                    ),
                    phone=f"+7916{i:07d}",
                    district=rng.choice(refs["districts"]),
                    status=status,
                    start_date=None if status == VolunteerStatus.APPLICATION else started,
                    leaving_reason=reason if status == VolunteerStatus.LEFT else None,
                    left_on=today if status == VolunteerStatus.LEFT else None,
                    search_name=normalize_name(last, first, middle),
                )
            )
        volunteers = Volunteer.objects.bulk_create(rows, batch_size=BATCH)
        through = Volunteer.skills.through
        through.objects.bulk_create(
            [
                through(volunteer_id=v.pk, skill_id=s.pk)
                for v in volunteers
                for s in rng.sample(refs["skills"], 2)
            ],
            batch_size=BATCH,
        )
        return volunteers

    def _beneficiaries(
        self, rng: random.Random, count: int, refs: dict[str, Any], staff: dict[str, list[User]]
    ) -> list[Beneficiary]:
        statuses = [CaseStatus.IN_WORK] * 5 + [CaseStatus.NEW, CaseStatus.PAUSED, CaseStatus.CLOSED]
        rows = []
        for i in range(count):
            last, first, middle = person(rng)
            status = rng.choice(statuses)
            rows.append(
                Beneficiary(
                    last_name=last,
                    first_name=first,
                    middle_name=middle,
                    birth_date=datetime.date(
                        1930 + rng.randrange(80), 1 + rng.randrange(12), 1 + rng.randrange(28)
                    ),
                    phone=f"+7903{i:07d}",
                    address=f"Москва, ул. Тестовая, д. {i % 300}",
                    category=rng.choice(refs["categories"]),
                    curator=rng.choice(staff["curator"]),
                    status=status,
                    closed_on=timezone.localdate() if status == CaseStatus.CLOSED else None,
                    outcome="Помощь оказана" if status == CaseStatus.CLOSED else "",
                    search_name=normalize_name(last, first, middle),
                )
            )
        # У закрытого случая в базе обязательна причина
        reason = None
        if any(b.status == CaseStatus.CLOSED for b in rows):
            from apps.core.models import CaseClosingReason

            reason = CaseClosingReason.objects.get_or_create(name="Помощь оказана")[0]
            for b in rows:
                if b.status == CaseStatus.CLOSED:
                    b.closing_reason = reason
        return Beneficiary.objects.bulk_create(rows, batch_size=BATCH)

    def _assistance(
        self,
        rng: random.Random,
        count: int,
        beneficiaries: list[Beneficiary],
        refs: dict[str, Any],
        staff: dict[str, list[User]],
    ) -> None:
        today = timezone.localdate()
        requests = HelpRequest.objects.bulk_create(
            [
                HelpRequest(
                    beneficiary=b,
                    assistance_type=rng.choice(refs["types"]),
                    description="Нужна помощь",
                    responsible=b.curator,
                    status=rng.choice(
                        [RequestStatus.NEW, RequestStatus.IN_WORK, RequestStatus.DONE]
                    ),
                    due_date=today + datetime.timedelta(days=rng.randrange(-30, 60)),
                )
                for b in beneficiaries
                if rng.random() < 0.5
            ],
            batch_size=BATCH,
        )
        self.stdout.write(f"Заявок: {len(requests)}")
        made = 0
        while made < count:
            size = min(BATCH * 4, count - made)
            batch = []
            for _ in range(size):
                b = rng.choice(beneficiaries)
                kind = rng.choice(refs["types"])
                money = kind.name == "Денежная помощь"
                batch.append(
                    Assistance(
                        beneficiary=b,
                        assistance_type=kind,
                        provided_on=today - datetime.timedelta(days=rng.randrange(3 * 365)),
                        quantity=None if money else Decimal(1 + rng.randrange(5)),
                        amount=Decimal(500 + rng.randrange(5000)) if money else None,
                        program=rng.choice(refs["programs"]),
                        executor_user=b.curator,
                    )
                )
            Assistance.objects.bulk_create(batch, batch_size=BATCH)
            made += size
            self.stdout.write(f"Помощь: {made}")

    def _shifts(
        self, rng: random.Random, volunteers: list[Volunteer], refs: dict[str, Any]
    ) -> None:
        now = timezone.now()
        starts = [
            now + datetime.timedelta(days=rng.randrange(-365, 60), hours=rng.randrange(8))
            for _ in range(2000)
        ]
        shifts = Shift.objects.bulk_create(
            [
                Shift(
                    title=f"Смена {i}",
                    shift_type=rng.choice(refs["shift_types"]),
                    starts_at=start,
                    ends_at=start + datetime.timedelta(hours=4),
                    address="Склад фонда",
                    volunteers_needed=5,
                    program=rng.choice(refs["programs"]),
                )
                for i, start in enumerate(starts)
            ],
            batch_size=BATCH,
        )
        active = [v for v in volunteers if v.status == VolunteerStatus.ACTIVE]
        rows = []
        for shift in shifts:
            for volunteer in rng.sample(active, 4):
                past = shift.ends_at < now
                rows.append(
                    Assignment(
                        kind=AssignmentKind.SHIFT,
                        shift=shift,
                        volunteer=volunteer,
                        status=AssignmentStatus.ATTENDED if past else AssignmentStatus.ASSIGNED,
                        hours=Decimal(4) if past else None,
                    )
                )
        Assignment.objects.bulk_create(rows, batch_size=BATCH)

    # --- сессии для Locust: вход с 2FA уже пройден ---
    def _sessions(self, staff: dict[str, list[User]]) -> list[dict[str, str]]:
        sessions = []
        for code, users in staff.items():
            for user in users:
                store = SessionStore()
                store[SESSION_KEY] = str(user.pk)
                store[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
                store[HASH_SESSION_KEY] = user.get_session_auth_hash()
                store[DEVICE_ID_SESSION_KEY] = SecureTOTPDevice.objects.get(user=user).persistent_id
                store.set_expiry(60 * 60 * 6)
                store.save()
                sessions.append({"role": code, "cookie": store.session_key or ""})
        return sessions
