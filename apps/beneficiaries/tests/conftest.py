import datetime
from collections.abc import Callable
from typing import Any

import pytest
from django.utils import timezone

from apps.beneficiaries import services
from apps.beneficiaries.models import Beneficiary
from apps.consents.models import ConsentKind, ConsentSigner
from apps.consents.services import create_beneficiary_consent
from apps.core.models import BeneficiaryCategory, BeneficiarySource, CaseClosingReason, Program

ADULT_BIRTH = datetime.date(1948, 5, 9)


def years_ago(years: int) -> datetime.date:
    today = timezone.localdate()
    try:
        return today.replace(year=today.year - years)
    except ValueError:  # 29 февраля
        return today.replace(year=today.year - years, day=28)


@pytest.fixture
def brefs(db) -> dict[str, Any]:
    return {
        "category": BeneficiaryCategory.objects.create(name="Пожилые одинокие"),
        "category2": BeneficiaryCategory.objects.create(name="Многодетные семьи"),
        "source": BeneficiarySource.objects.create(name="Соцзащита"),
        "reason": CaseClosingReason.objects.create(name="Помощь оказана"),
        "program": Program.objects.create(name="Помощь пожилым"),
    }


@pytest.fixture
def curator(make_user) -> Any:
    return make_user("curator", last_name="Кураторова", first_name="Анна")


@pytest.fixture
def make_beneficiary(db, curator) -> Callable[..., Beneficiary]:
    counter = {"n": 0}

    def factory(**fields: Any) -> Beneficiary:
        counter["n"] += 1
        n = counter["n"]
        programs = fields.pop("programs", ())
        owner = fields.pop("curator", curator)
        data = {
            "last_name": f"Смирнов{n}",
            "first_name": "Пётр",
            "middle_name": "Ильич",
            "birth_date": ADULT_BIRTH,
            "address": "Москва, ул. Ленина, д. 1",
            "phone": f"+7903{n:07d}",
            **fields,
        }
        return services.create_beneficiary(
            data, curator=owner, programs=programs, confirm_duplicates=True
        )

    return factory


def give_consent(beneficiary: Beneficiary, **fields: Any) -> Any:
    defaults: dict[str, Any] = {"signed_on": timezone.localdate(), "kind": ConsentKind.GENERAL}
    if beneficiary.is_minor:
        defaults |= {"signer": ConsentSigner.REPRESENTATIVE, "signer_name": "Иванова Ольга"}
    return create_beneficiary_consent(beneficiary, **(defaults | fields))


def to_in_work(beneficiary: Beneficiary) -> Beneficiary:
    give_consent(beneficiary)
    services.change_status(beneficiary, "assessment")
    services.change_status(beneficiary, "in_work")
    return beneficiary
