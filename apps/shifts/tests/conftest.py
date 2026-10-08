import datetime
from collections.abc import Callable
from typing import Any

import pytest
from django.utils import timezone

from apps.beneficiaries.tests.conftest import (  # noqa: F401 — фикстуры модуля «Подопечные»
    brefs,
    curator,
    make_beneficiary,
    to_in_work,
)
from apps.core.models import ShiftType
from apps.shifts import services
from apps.shifts.models import Shift
from apps.volunteers import services as volunteer_services
from apps.volunteers.models import Volunteer
from apps.volunteers.tests.conftest import (  # noqa: F401
    make_ready_for_activation,
    make_volunteer,
    refs,
)

NOW = timezone.now
DAY = datetime.timedelta(days=1)
HOUR = datetime.timedelta(hours=1)


@pytest.fixture
def coordinator(make_user) -> Any:
    return make_user("coordinator", last_name="Координаторова", first_name="Ольга")


@pytest.fixture
def shift_type(db) -> ShiftType:
    return ShiftType.objects.create(name="Раздача продуктов")


@pytest.fixture
def make_active(make_volunteer, refs) -> Callable[..., Volunteer]:  # noqa: F811
    """Волонтёр в статусе «Активен»."""

    def factory(**fields: Any) -> Volunteer:
        volunteer = make_ready_for_activation(make_volunteer(**fields), refs)
        volunteer_services.change_status(volunteer, "active")
        return volunteer

    return factory


@pytest.fixture
def make_shift(db, shift_type) -> Callable[..., Shift]:
    def factory(*, starts_in: datetime.timedelta = DAY, hours: int = 4, **fields: Any) -> Shift:
        skills = fields.pop("skills", ())
        starts = (NOW() + starts_in).replace(second=0, microsecond=0)
        data = {
            "title": "Раздача продуктов",
            "shift_type": shift_type,
            "starts_at": starts,
            "ends_at": starts + datetime.timedelta(hours=hours),
            "address": "Москва, ул. Мира, д. 5",
            "volunteers_needed": 3,
            "description": "",
            "allow_signup": True,
            **fields,
        }
        return services.create_shift(data, skills=skills)

    return factory


@pytest.fixture
def in_work_beneficiary(make_beneficiary) -> Any:  # noqa: F811
    return to_in_work(make_beneficiary())


def past_shift(shift: Shift, *, started_ago: datetime.timedelta = 5 * HOUR) -> Shift:
    """Сдвинуть смену в прошлое в обход сервиса (смены в прошлом создавать можно и так,
    но так короче для проверок «после начала»)."""
    duration = shift.ends_at - shift.starts_at
    Shift.objects.filter(pk=shift.pk).update(
        starts_at=NOW() - started_ago, ends_at=NOW() - started_ago + duration
    )
    shift.refresh_from_db()
    return shift
