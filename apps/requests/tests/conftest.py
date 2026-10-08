import datetime
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from apps.beneficiaries.tests.conftest import (  # noqa: F401 — фикстуры модуля «Подопечные»
    brefs,
    curator,
    make_beneficiary,
)
from apps.core.models import AssistanceType
from apps.requests import services
from apps.requests.models import HelpRequest
from apps.volunteers.tests.conftest import make_volunteer, refs  # noqa: F401

TODAY = timezone.localdate


@pytest.fixture
def food(db) -> AssistanceType:
    return AssistanceType.objects.create(name="Продуктовый набор", unit="шт.")


@pytest.fixture
def make_request(db, make_beneficiary, food) -> Callable[..., HelpRequest]:  # noqa: F811
    def factory(beneficiary: Any = None, **fields: Any) -> HelpRequest:
        responsible = fields.pop("responsible", None)
        data = {
            "assistance_type": food,
            "description": "Нужен продуктовый набор",
            "due_date": TODAY() + datetime.timedelta(days=7),
            **fields,
        }
        return services.create_request(
            beneficiary or make_beneficiary(), data, responsible=responsible
        )

    return factory


def assistance_data(food: AssistanceType, executor: Any, **fields: Any) -> dict[str, Any]:
    return {
        "assistance_type": food,
        "provided_on": TODAY(),
        "quantity": Decimal(1),
        "amount": None,
        "executor_user": executor,
        "executor_volunteer": None,
        **fields,
    }
