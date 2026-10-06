"""Бизнес-правила модуля «Волонтёры»: ФТ-В-01…08, п. 4.3 ТЗ."""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.models import AuditAction, AuditLog
from apps.consents.models import ConsentSigner
from apps.consents.services import revoke_consent
from apps.core.context import acting_as
from apps.volunteers import services
from apps.volunteers.models import Volunteer, VolunteerStatus, VolunteerStatusChange

from .conftest import ADULT_BIRTH, give_consent, make_ready_for_activation, years_ago

pytestmark = pytest.mark.django_db
S = VolunteerStatus
TODAY = timezone.localdate


# --- ФТ-В-01: карточка ----------------------------------------------------------------------


def test_ft_v_01_create_normalizes_contacts(refs, make_user):
    coordinator = make_user("coordinator")
    with acting_as(coordinator):
        v = services.create_volunteer(
            {
                "last_name": " Иванова ",
                "first_name": "Мария",
                "birth_date": ADULT_BIRTH,
                "phone": "8 (916) 123-45-67",
                "email": " Maria@Example.ORG ",
                "telegram_username": "@maria_ivanova",
                "district": refs["district"],
            },
            skills=[refs["skill_drive"]],
            availability=[(2, datetime.time(18), datetime.time(21))],
        )
    v.refresh_from_db()
    assert v.status == S.APPLICATION
    assert v.phone == "+79161234567"
    assert v.email == "maria@example.org"
    assert v.telegram_username == "maria_ivanova"
    assert v.last_name == "Иванова"
    assert v.search_name == "иванова мария"
    assert v.created_by == coordinator
    assert list(v.skills.all()) == [refs["skill_drive"]]
    assert v.availability.get().weekday == 2
    # Начальный статус попадает в журнал статусов (воронка ФТ-О-03) и в аудит
    assert v.status_changes.get().to_status == S.APPLICATION
    assert AuditLog.objects.filter(action=AuditAction.CREATE, object_id=str(v.pk)).exists()
    assert v.history.count() == 1


def test_ft_v_01_invalid_phone_rejected(db):
    with pytest.raises(ValidationError) as exc:
        services.create_volunteer(
            {"last_name": "А", "first_name": "Б", "birth_date": ADULT_BIRTH, "phone": "12345"}
        )
    assert "phone" in exc.value.message_dict


def test_ft_v_01_birth_date_in_future_rejected(db):
    with pytest.raises(ValidationError) as exc:
        services.create_volunteer(
            {
                "last_name": "А",
                "first_name": "Б",
                "birth_date": TODAY() + datetime.timedelta(days=1),
                "phone": "+79161234567",
            }
        )
    assert "birth_date" in exc.value.message_dict


def test_update_audits_only_changed_fields(make_volunteer, refs):
    v = make_volunteer()
    services.update_volunteer(
        v,
        {"first_name": "Анна", "city": "Москва"},
        skills=[refs["skill_med"]],
        availability=[],
    )
    log = AuditLog.objects.filter(action=AuditAction.UPDATE, object_id=str(v.pk)).get()
    assert log.fields == ["city", "skills"]
    services.update_volunteer(v, {"city": "Москва"})  # ничего не изменилось
    assert AuditLog.objects.filter(action=AuditAction.UPDATE, object_id=str(v.pk)).count() == 1


# --- ФТ-В-02, п. 4.3: статусы -----------------------------------------------------------------

FORWARD = [
    (S.APPLICATION, S.INTERVIEW),
    (S.INTERVIEW, S.TRAINING),
    (S.TRAINING, S.ACTIVE),
    (S.ACTIVE, S.PAUSED),
    (S.PAUSED, S.ACTIVE),
]


def test_ft_v_02_transition_table_matches_spec():
    allowed = {(a, b) for a, targets in services.TRANSITIONS.items() for b in targets}
    to_left = {(a, S.LEFT) for a in S.values if a != S.LEFT}
    assert allowed == set(FORWARD) | to_left
    assert services.TRANSITIONS[S.LEFT] == frozenset()


def test_ft_v_02_full_path_to_active_and_pause(make_volunteer, refs):
    v = make_ready_for_activation(make_volunteer(), refs)
    services.change_status(v, S.ACTIVE)
    assert v.start_date == TODAY()
    services.change_status(v, S.PAUSED)
    services.change_status(v, S.ACTIVE)
    v.refresh_from_db()
    assert v.status == S.ACTIVE and v.start_date == TODAY()
    history = list(v.status_changes.order_by("created_at").values_list("from_status", "to_status"))
    assert history == [
        ("", S.APPLICATION),
        (S.APPLICATION, S.INTERVIEW),
        (S.INTERVIEW, S.TRAINING),
        (S.TRAINING, S.ACTIVE),
        (S.ACTIVE, S.PAUSED),
        (S.PAUSED, S.ACTIVE),
    ]
    log = AuditLog.objects.filter(action=AuditAction.UPDATE, object_id=str(v.pk)).first()
    assert log is not None and log.extra == {"from": S.PAUSED, "to": S.ACTIVE}


@pytest.mark.parametrize(
    ("start_path", "target"),
    [
        ([], S.TRAINING),  # анкета → обучение (пропуск собеседования)
        ([], S.ACTIVE),
        ([S.INTERVIEW], S.APPLICATION),  # назад нельзя
        ([S.INTERVIEW], S.ACTIVE),
        ([S.INTERVIEW, S.TRAINING], S.PAUSED),
        ([S.INTERVIEW, S.TRAINING], S.INTERVIEW),
    ],
)
def test_ft_v_02_forbidden_transitions(start_path, target, make_volunteer, refs):
    v = make_volunteer()
    give_consent(v)
    services.add_training(v, training_type=refs["training"], completed_on=TODAY())
    for status in start_path:
        services.change_status(v, status)
    with pytest.raises(ValidationError, match="не предусмотрен"):
        services.change_status(v, target)


def test_ft_v_02_unknown_status_rejected(make_volunteer):
    with pytest.raises(ValidationError, match="Неизвестный статус"):
        services.change_status(make_volunteer(), "deleted")


@pytest.mark.parametrize("path", [[], [S.INTERVIEW], [S.INTERVIEW, S.TRAINING]])
def test_ft_v_02_any_status_to_left_requires_reason(path, make_volunteer, refs):
    v = make_volunteer()
    for status in path:
        services.change_status(v, status)
    with pytest.raises(ValidationError) as exc:
        services.change_status(v, S.LEFT)
    assert "reason" in exc.value.message_dict
    services.change_status(v, S.LEFT, reason=refs["reason"], comment="Уехал в другой город")
    v.refresh_from_db()
    assert v.status == S.LEFT and v.leaving_reason == refs["reason"] and v.left_on == TODAY()
    change = v.status_changes.first()
    assert change is not None and change.reason == refs["reason"]


def test_ft_v_02_left_is_final(make_volunteer, refs):
    v = make_volunteer()
    services.change_status(v, S.LEFT, reason=refs["reason"])
    assert services.allowed_transitions(v) == []
    with pytest.raises(ValidationError):
        services.change_status(v, S.APPLICATION)


def test_left_without_reason_blocked_by_database(make_volunteer):
    v = make_volunteer()
    with pytest.raises(IntegrityError), transaction.atomic():
        Volunteer.objects.filter(pk=v.pk).update(status=S.LEFT)


def test_archived_volunteer_status_cannot_change(make_volunteer):
    v = make_volunteer()
    services.archive_volunteer(v)
    with pytest.raises(ValidationError, match="в архиве"):
        services.change_status(v, S.INTERVIEW)


# --- п. 4.3: условия для «Активен» ------------------------------------------------------------


def _to_training(v):
    services.change_status(v, S.INTERVIEW)
    services.change_status(v, S.TRAINING)


def test_active_requires_consent(make_volunteer, refs):
    v = make_volunteer()
    services.add_training(v, training_type=refs["training"], completed_on=TODAY())
    _to_training(v)
    with pytest.raises(ValidationError, match="согласия"):
        services.change_status(v, S.ACTIVE)
    give_consent(v)
    services.change_status(v, S.ACTIVE)


def test_active_requires_training(make_volunteer, refs):
    v = make_volunteer()
    give_consent(v)
    _to_training(v)
    with pytest.raises(ValidationError, match="обучение"):
        services.change_status(v, S.ACTIVE)


@pytest.mark.parametrize(
    "consent_fields",
    [
        {"signed_on": datetime.date(2020, 1, 1), "valid_until": datetime.date(2021, 1, 1)},
        {"kind": "photo_video"},  # согласие на съёмку — не согласие на обработку ПДн
    ],
)
def test_active_requires_valid_general_consent(consent_fields, make_volunteer, refs):
    v = make_volunteer()
    give_consent(v, **consent_fields)
    services.add_training(v, training_type=refs["training"], completed_on=TODAY())
    _to_training(v)
    with pytest.raises(ValidationError, match="согласия"):
        services.change_status(v, S.ACTIVE)


def test_revoked_consent_blocks_return_from_pause(make_volunteer, refs):
    v = make_ready_for_activation(make_volunteer(), refs)
    services.change_status(v, S.ACTIVE)
    services.change_status(v, S.PAUSED)
    revoke_consent(v.consents.get(), revoked_on=TODAY())
    with pytest.raises(ValidationError, match="согласия"):
        services.change_status(v, S.ACTIVE)


# --- ФТ-В-08: несовершеннолетние --------------------------------------------------------------


def test_ft_v_08_minor_requires_guardian(db):
    with pytest.raises(ValidationError) as exc:
        services.create_volunteer(
            {
                "last_name": "Юная",
                "first_name": "Ира",
                "birth_date": years_ago(16),
                "phone": "+79161234567",
            }
        )
    assert {"guardian_name", "guardian_phone"} <= set(exc.value.message_dict)


def test_ft_v_08_adult_on_18th_birthday(make_volunteer):
    assert not make_volunteer(birth_date=years_ago(18)).is_minor
    minor = make_volunteer(
        birth_date=years_ago(18, days=-1), guardian_name="Мать", guardian_phone="+79160000099"
    )
    assert minor.is_minor


def test_ft_v_08_minor_needs_representative_consent(make_volunteer, refs):
    v = make_volunteer(
        birth_date=years_ago(16), guardian_name="Петрова Ольга", guardian_phone="+79160000099"
    )
    give_consent(v, signer=ConsentSigner.SUBJECT, signer_name="")  # подписал сам — не годится
    services.add_training(v, training_type=refs["training"], completed_on=TODAY())
    _to_training(v)
    with pytest.raises(ValidationError, match="законным представителем"):
        services.change_status(v, S.ACTIVE)
    give_consent(v)  # от представителя
    services.change_status(v, S.ACTIVE)


def test_ft_v_08_representative_consent_needs_name(make_volunteer):
    v = make_volunteer()
    with pytest.raises(ValidationError) as exc:
        give_consent(v, signer=ConsentSigner.REPRESENTATIVE, signer_name=" ")
    assert "signer_name" in exc.value.message_dict


# --- ФТ-В-04: дубли ---------------------------------------------------------------------------


def _data(**fields):
    return {
        "last_name": "Смирнова",
        "first_name": "Елена",
        "middle_name": "Павловна",
        "birth_date": ADULT_BIRTH,
        "phone": "+79035550011",
        "email": "lena@example.org",
    } | fields


def test_ft_v_04_duplicate_by_phone(db):
    original = services.create_volunteer(_data())
    with pytest.raises(services.DuplicateVolunteerError) as exc:
        services.create_volunteer(
            _data(
                last_name="Другая",
                email="",
                birth_date=datetime.date(1980, 1, 1),
                phone="8 903 555-00-11",
            )
        )
    [match] = exc.value.matches
    assert match.person == original and match.reasons == ["телефон"]


def test_ft_v_04_duplicate_by_email_case_insensitive(db):
    services.create_volunteer(_data())
    with pytest.raises(services.DuplicateVolunteerError) as exc:
        services.create_volunteer(
            _data(
                last_name="Другая",
                phone="+79035550099",
                email="LENA@example.org",
                birth_date=datetime.date(1980, 1, 1),
            )
        )
    assert exc.value.matches[0].reasons == ["email"]


@pytest.mark.parametrize(
    ("last", "first", "middle"),
    [
        ("Смирнова", "Елена", "Павловна"),
        ("смирнова", "елена", "павловна"),
        ("Смирнова", "Елена", ""),  # без отчества
        ("Смирнова", "Алёна", "Павловна"),  # опечатка в имени
    ],
)
def test_ft_v_04_duplicate_by_name_and_birth_date(last, first, middle, db):
    services.create_volunteer(_data())
    with pytest.raises(services.DuplicateVolunteerError) as exc:
        services.create_volunteer(
            _data(
                last_name=last, first_name=first, middle_name=middle, phone="+79035550099", email=""
            )
        )
    assert exc.value.matches[0].reasons == ["ФИО и дата рождения"]


def test_ft_v_04_reasons_are_exact(db):
    """Совпал телефон и случайно дата рождения, ФИО другое — причина только «телефон»."""
    services.create_volunteer(_data())
    with pytest.raises(services.DuplicateVolunteerError) as exc:
        services.create_volunteer(
            _data(last_name="Кузнецов", first_name="Пётр", middle_name="", email="")
        )
    assert exc.value.matches[0].reasons == ["телефон"]


def test_archived_card_is_read_only(make_volunteer, refs):
    v = make_volunteer()
    services.archive_volunteer(v)
    with pytest.raises(ValidationError, match="в архиве"):
        services.update_volunteer(v, {"city": "Тула"})
    with pytest.raises(ValidationError, match="в архиве"):
        services.add_training(v, training_type=refs["training"], completed_on=TODAY())
    with pytest.raises(ValidationError, match="в архиве"):
        services.add_manual_hours(v, worked_on=TODAY(), hours=Decimal(1), comment="x")


def test_ft_v_04_namesake_with_other_birth_date_is_not_duplicate(db):
    services.create_volunteer(_data())
    services.create_volunteer(
        _data(phone="+79035550099", email="", birth_date=datetime.date(1990, 5, 5))
    )
    assert Volunteer.objects.count() == 2


def test_ft_v_04_archived_cards_are_checked_too(db):
    original = services.create_volunteer(_data())
    services.archive_volunteer(original)
    with pytest.raises(services.DuplicateVolunteerError):
        services.create_volunteer(_data(email=""))


def test_ft_v_04_confirmed_duplicate_is_saved(db):
    services.create_volunteer(_data())
    services.create_volunteer(_data(), confirm_duplicates=True)
    assert Volunteer.objects.count() == 2
    log = AuditLog.objects.filter(action=AuditAction.CREATE).first()
    assert log is not None and log.extra == {"duplicates_confirmed": True}


# --- ФТ-В-06, ФТ-В-07: обучение и часы ---------------------------------------------------------


def test_ft_v_06_training_with_date(make_volunteer, refs):
    v = make_volunteer()
    training = services.add_training(
        v, training_type=refs["training"], completed_on=TODAY(), conducted_by="  Иванов  И.  "
    )
    assert training.conducted_by == "Иванов И."
    with pytest.raises(ValidationError):
        services.add_training(
            v, training_type=refs["training"], completed_on=TODAY() + datetime.timedelta(days=1)
        )


def test_ft_v_07_manual_hours_need_comment(make_volunteer, refs):
    v = make_volunteer()
    with pytest.raises(ValidationError) as exc:
        services.add_manual_hours(v, worked_on=TODAY(), hours=Decimal(2), comment="  ")
    assert "comment" in exc.value.message_dict
    services.add_manual_hours(
        v, worked_on=TODAY(), hours=Decimal("2.5"), comment="Ярмарка", program=refs["program"]
    )
    services.add_manual_hours(v, worked_on=TODAY(), hours=Decimal(1), comment="Звонки")
    assert services.total_hours(v) == Decimal("3.5")


@pytest.mark.parametrize("hours", [Decimal(0), Decimal("-1"), Decimal("24.5")])
def test_ft_v_07_hours_range(hours, make_volunteer):
    with pytest.raises(ValidationError):
        services.add_manual_hours(make_volunteer(), worked_on=TODAY(), hours=hours, comment="x")


def test_status_change_is_serialized(make_volunteer):
    """Повторная попытка того же перехода (двойной клик, два координатора) отклоняется."""
    v = make_volunteer()
    stale = Volunteer.objects.get(pk=v.pk)
    services.change_status(v, S.INTERVIEW)
    with pytest.raises(ValidationError):
        services.change_status(stale, S.INTERVIEW)  # из «Собеседования» в него же нельзя
    assert VolunteerStatusChange.objects.filter(volunteer=v, to_status=S.INTERVIEW).count() == 1
