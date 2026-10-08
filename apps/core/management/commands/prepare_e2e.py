"""Данные для сквозных тестов в браузере (Playwright, e2e/): отдельная база *_e2e.

Сотрудники всех ролей с известным паролем и секретом TOTP — тест вычисляет код сам,
вход проходит по-настоящему (пароль + 2FA). Плюс справочники и по одной карточке.
Работает только с базой, имя которой оканчивается на «_e2e» (scripts/e2e/run.sh).
"""

from __future__ import annotations

import datetime
import os
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.models import Role, SecureTOTPDevice, User, UserRole
from apps.beneficiaries import services as beneficiary_services
from apps.consents.models import ConsentKind
from apps.consents.services import create_beneficiary_consent, create_volunteer_consent
from apps.core.models import (
    AssistanceType,
    BeneficiaryCategory,
    BeneficiarySource,
    District,
    Program,
    ShiftType,
    Skill,
    TrainingType,
)
from apps.shifts import services as shift_services
from apps.volunteers import services as volunteer_services

ROLES = ("admin", "coordinator", "curator", "analyst")
# Для каждой роли — несколько учётных записей: код TOTP нельзя использовать дважды
ACCOUNTS_PER_ROLE = 6


class Command(BaseCommand):
    help = "Подготовить базу *_e2e для сквозных тестов в браузере."

    def handle(self, *args: Any, **options: Any) -> None:
        if not str(connection.settings_dict["NAME"]).endswith("_e2e"):
            raise CommandError("Только для базы *_e2e (scripts/e2e/run.sh).")
        password = os.environ.get("E2E_PASSWORD", "E2e-проверка-2026")
        key = os.environ.get("E2E_TOTP_KEY", "3132333435363738393031323334353637383930")
        with transaction.atomic():
            roles = {r.code: r for r in Role.objects.all()}
            users: dict[str, User] = {}
            for code in ROLES:
                for i in range(ACCOUNTS_PER_ROLE):
                    user = User.objects.create_user(
                        username=f"e2e-{code}-{i}",
                        password=password,
                        last_name="Тестова",
                        first_name=code.capitalize(),
                    )
                    UserRole.objects.create(user=user, role=roles[code])
                    SecureTOTPDevice.objects.create(user=user, name="e2e", confirmed=True, key=key)
                    users.setdefault(code, user)
            self._data(users["curator"])
        self.stdout.write("База e2e готова.")

    def _data(self, curator: User) -> None:
        district = District.objects.create(name="Центральный", city="Москва")
        skill = Skill.objects.create(name="Вождение")
        Program.objects.create(name="Тёплый дом")
        category = BeneficiaryCategory.objects.create(name="Пожилые одинокие")
        BeneficiarySource.objects.create(name="Соцзащита")
        AssistanceType.objects.create(name="Продуктовый набор", unit="шт.")
        shift_type = ShiftType.objects.create(name="Раздача продуктов")
        training = TrainingType.objects.create(name="Вводный инструктаж")
        today = timezone.localdate()

        volunteer = volunteer_services.create_volunteer(
            {
                "last_name": "Волонтёрова",
                "first_name": "Анна",
                "birth_date": datetime.date(1995, 3, 14),
                "phone": "+79160000001",
                "district": district,
            },
            skills=[skill],
            confirm_duplicates=True,
        )
        create_volunteer_consent(volunteer, signed_on=today)
        volunteer_services.add_training(volunteer, training_type=training, completed_on=today)
        for status in ("interview", "training", "active"):
            volunteer_services.change_status(volunteer, status)

        beneficiary = beneficiary_services.create_beneficiary(
            {
                "last_name": "Подопечнова",
                "first_name": "Галина",
                "middle_name": "Петровна",
                "birth_date": datetime.date(1941, 3, 12),
                "address": "Москва, ул. Садовая, д. 3",
                "phone": "+79031112233",
                "category": category,
            },
            curator=curator,
            confirm_duplicates=True,
        )
        create_beneficiary_consent(beneficiary, signed_on=today, kind=ConsentKind.GENERAL)
        for status in ("assessment", "in_work"):
            beneficiary_services.change_status(beneficiary, status)

        starts = timezone.now() + datetime.timedelta(days=2)
        shift_services.create_shift(
            {
                "title": "Раздача продуктовых наборов",
                "shift_type": shift_type,
                "starts_at": starts,
                "ends_at": starts + datetime.timedelta(hours=4),
                "address": "Склад фонда, ул. Мира, 5",
                "volunteers_needed": 4,
            }
        )
