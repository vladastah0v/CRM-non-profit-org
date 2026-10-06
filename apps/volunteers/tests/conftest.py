import datetime
from collections.abc import Callable
from typing import Any

import pytest
from django.utils import timezone

from apps.consents.models import ConsentSigner
from apps.consents.services import create_volunteer_consent
from apps.core.models import District, LeavingReason, Program, Skill, TrainingType
from apps.volunteers import services
from apps.volunteers.models import Volunteer

ADULT_BIRTH = datetime.date(1995, 3, 14)


def years_ago(years: int, days: int = 0) -> datetime.date:
    today = timezone.localdate()
    try:
        anniversary = today.replace(year=today.year - years)
    except ValueError:  # 29 февраля
        anniversary = today.replace(year=today.year - years, day=28)
    return anniversary - datetime.timedelta(days=days)


@pytest.fixture
def refs(db) -> dict[str, Any]:
    return {
        "skill_drive": Skill.objects.create(name="Вождение"),
        "skill_med": Skill.objects.create(name="Первая помощь"),
        "district": District.objects.create(name="Центральный", city="Москва"),
        "district2": District.objects.create(name="Северный", city="Москва"),
        "program": Program.objects.create(name="Тёплый дом"),
        "training": TrainingType.objects.create(name="Вводный инструктаж"),
        "reason": LeavingReason.objects.create(name="Переезд"),
    }


@pytest.fixture
def make_volunteer(db) -> Callable[..., Volunteer]:
    counter = {"n": 0}

    def factory(**fields: Any) -> Volunteer:
        counter["n"] += 1
        n = counter["n"]
        skills = fields.pop("skills", ())
        programs = fields.pop("programs", ())
        availability = fields.pop("availability", ())
        data = {
            "last_name": f"Петрова{n}",
            "first_name": "Анна",
            "middle_name": "Сергеевна",
            "birth_date": ADULT_BIRTH,
            "phone": f"+7916{n:07d}",
            **fields,
        }
        return services.create_volunteer(
            data,
            skills=skills,
            programs=programs,
            availability=availability,
            confirm_duplicates=True,
        )

    return factory


def give_consent(volunteer: Volunteer, **fields: Any) -> Any:
    defaults: dict[str, Any] = {"signed_on": timezone.localdate()}
    if volunteer.is_minor:
        defaults |= {"signer": ConsentSigner.REPRESENTATIVE, "signer_name": "Петрова Ольга"}
    return create_volunteer_consent(volunteer, **(defaults | fields))


def make_ready_for_activation(volunteer: Volunteer, refs: dict[str, Any]) -> Volunteer:
    """Довести до статуса «Обучение» с согласием и пройденным обучением."""
    give_consent(volunteer)
    services.add_training(
        volunteer, training_type=refs["training"], completed_on=timezone.localdate()
    )
    services.change_status(volunteer, "interview")
    services.change_status(volunteer, "training")
    return volunteer
