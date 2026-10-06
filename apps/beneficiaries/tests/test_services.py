"""Бизнес-правила модуля «Подопечные»: ФТ-П-01…06, ФТ-Д-01…03, п. 4.3 ТЗ."""

import datetime
import io

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.audit.models import AuditAction, AuditLog
from apps.beneficiaries import services
from apps.beneficiaries.models import Beneficiary, CaseStatus, RelatedPerson
from apps.consents.models import ConsentKind, ConsentSigner
from apps.core.context import acting_as
from apps.notifications.models import Notification, NotificationKind

from .conftest import ADULT_BIRTH, give_consent, to_in_work, years_ago

pytestmark = pytest.mark.django_db
S = CaseStatus
TODAY = timezone.localdate


# --- ФТ-П-01: карточка ----------------------------------------------------------------------


def test_ft_p_01_create(brefs, curator, make_user):
    admin = make_user("admin")
    with acting_as(admin):
        b = services.create_beneficiary(
            {
                "last_name": " Соколова ",
                "first_name": "Нина",
                "birth_date": ADULT_BIRTH,
                "address": "Москва,  ул. Мира, 5",
                "phone": "8 903 111-22-33",
                "category": brefs["category"],
                "source": brefs["source"],
            },
            curator=curator,
            programs=[brefs["program"]],
        )
    b.refresh_from_db()
    assert b.status == S.NEW and b.curator == curator
    assert b.phone == "+79031112233" and b.address == "Москва, ул. Мира, 5"
    assert b.search_name == "соколова нина" and b.created_by == admin
    assert b.status_changes.get().to_status == S.NEW
    assert b.history.count() == 1
    assert AuditLog.objects.filter(action=AuditAction.CREATE, object_id=str(b.pk)).exists()
    # Куратору, которого назначил другой сотрудник, приходит уведомление
    note = Notification.objects.get(recipient=curator)
    assert note.kind == NotificationKind.CASE_ASSIGNED and note.url == b.get_absolute_url()


def test_no_notification_when_curator_creates_own_case(curator, make_beneficiary):
    with acting_as(curator):
        make_beneficiary()
    assert not Notification.objects.filter(recipient=curator).exists()


def test_phone_is_optional(curator):
    b = services.create_beneficiary(
        {"last_name": "Без", "first_name": "Телефона", "birth_date": ADULT_BIRTH}, curator=curator
    )
    assert b.phone == ""


def test_curator_must_have_curator_role(make_user):
    with pytest.raises(ValidationError) as exc:
        services.create_beneficiary(
            {"last_name": "А", "first_name": "Б", "birth_date": ADULT_BIRTH},
            curator=make_user("coordinator"),
        )
    assert "curator" in exc.value.message_dict


# --- ФТ-П-04, п. 4.3: статусы случая -----------------------------------------------------------


def test_ft_p_04_transition_table_matches_spec():
    forward = {
        (S.NEW, S.ASSESSMENT),
        (S.ASSESSMENT, S.IN_WORK),
        (S.IN_WORK, S.PAUSED),
        (S.PAUSED, S.IN_WORK),
    }
    to_closed = {(a, S.CLOSED) for a in S.values if a != S.CLOSED}
    allowed = {(a, b) for a, targets in services.TRANSITIONS.items() for b in targets}
    assert allowed == forward | to_closed
    assert services.TRANSITIONS[S.CLOSED] == frozenset()


def test_ft_p_04_full_path(make_beneficiary, brefs):
    b = to_in_work(make_beneficiary())
    services.change_status(b, S.PAUSED)
    services.change_status(b, S.IN_WORK)
    services.change_status(b, S.CLOSED, reason=brefs["reason"], outcome="Продукты доставлены")
    b.refresh_from_db()
    assert b.status == S.CLOSED and b.closed_on == TODAY() and b.outcome == "Продукты доставлены"
    assert services.allowed_transitions(b) == []
    steps = list(b.status_changes.order_by("created_at").values_list("to_status", flat=True))
    assert steps == [S.NEW, S.ASSESSMENT, S.IN_WORK, S.PAUSED, S.IN_WORK, S.CLOSED]


@pytest.mark.parametrize(
    ("path", "target"),
    [([], S.IN_WORK), ([], S.PAUSED), ([S.ASSESSMENT], S.NEW), ([S.ASSESSMENT], S.PAUSED)],
)
def test_ft_p_04_forbidden(path, target, make_beneficiary):
    b = make_beneficiary()
    give_consent(b)
    for status in path:
        services.change_status(b, status)
    with pytest.raises(ValidationError, match="не предусмотрен"):
        services.change_status(b, target)


def test_ft_p_04_closing_requires_reason_and_outcome(make_beneficiary, brefs):
    b = make_beneficiary()
    with pytest.raises(ValidationError) as exc:
        services.change_status(b, S.CLOSED, outcome="  ")
    assert {"reason", "outcome"} <= set(exc.value.message_dict)
    with pytest.raises(IntegrityError), transaction.atomic():
        Beneficiary.objects.filter(pk=b.pk).update(status=S.CLOSED)


def test_in_work_requires_consent_and_curator(make_beneficiary, make_user):
    b = make_beneficiary(curator=None)
    services.change_status(b, S.ASSESSMENT)
    with pytest.raises(ValidationError) as exc:
        services.change_status(b, S.IN_WORK)
    assert "согласия" in str(exc.value) and "куратор" in str(exc.value)
    give_consent(b)
    services.assign_curator(b, make_user("curator"))
    services.change_status(b, S.IN_WORK)


def test_in_work_needs_general_not_photo_consent(make_beneficiary):
    b = make_beneficiary()
    give_consent(b, kind=ConsentKind.PHOTO_VIDEO)
    services.change_status(b, S.ASSESSMENT)
    with pytest.raises(ValidationError, match="согласия"):
        services.change_status(b, S.IN_WORK)


def test_minor_needs_representative(make_beneficiary):
    b = make_beneficiary(birth_date=years_ago(10))
    give_consent(b, signer=ConsentSigner.SUBJECT, signer_name="")
    services.change_status(b, S.ASSESSMENT)
    with pytest.raises(ValidationError) as exc:
        services.change_status(b, S.IN_WORK)
    assert "законным представителем" in str(exc.value)
    assert "законный представитель несовершеннолетнего" in str(exc.value)
    give_consent(b)  # от представителя
    services.add_related_person(
        b, {"full_name": "Иванова Ольга", "relation": "мать", "is_legal_representative": True}
    )
    services.change_status(b, S.IN_WORK)


# --- ФТ-Д-02, ФТ-Д-03 --------------------------------------------------------------------------


def test_ft_d_02_in_work_card_not_saved_without_valid_consent(make_beneficiary):
    b = make_beneficiary()
    give_consent(b, signed_on=datetime.date(2020, 1, 1), valid_until=TODAY())
    services.change_status(b, S.ASSESSMENT)
    services.change_status(b, S.IN_WORK)
    # Срок согласия истёк — сохранить карточку в работе нельзя
    tomorrow = TODAY() + datetime.timedelta(days=1)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(timezone, "localdate", lambda *a, **k: tomorrow)
        with pytest.raises(ValidationError, match="без действующего согласия"):
            services.update_beneficiary(b, {"address": "Новый адрес"})
        with pytest.raises(ValidationError, match="без действующего согласия"):
            services.save_sensitive(b, {"passport": "4510 123456"})
        services.change_status(b, S.PAUSED)
        services.update_beneficiary(b, {"address": "Новый адрес"})  # на паузе можно


def test_ft_d_03_revocation_pauses_case_and_notifies_admin(make_beneficiary, make_user):
    admin = make_user("admin")
    b = to_in_work(make_beneficiary())
    consent = b.consents.get()
    services.revoke_beneficiary_consent(consent, revoked_on=TODAY(), note="Заявление")
    b.refresh_from_db()
    assert b.status == S.PAUSED
    last_change = b.status_changes.first()
    assert last_change is not None and "отозвано согласие" in last_change.comment
    note = Notification.objects.get(recipient=admin, kind=NotificationKind.CONSENT_REVOKED)
    assert note.url == b.get_absolute_url() and "обезличивание" in note.body


def test_revocation_keeps_work_if_other_consent_valid(make_beneficiary):
    b = to_in_work(make_beneficiary())
    second = give_consent(b)
    services.revoke_beneficiary_consent(second, revoked_on=TODAY())
    b.refresh_from_db()
    assert b.status == S.IN_WORK


# --- ФТ-П-03: защищённый блок ------------------------------------------------------------------


def test_ft_p_03_health_requires_special_consent(make_beneficiary):
    b = make_beneficiary()
    with pytest.raises(ValidationError) as exc:
        services.save_sensitive(b, {"health": "Диабет 2 типа", "passport": "4510 123456"})
    assert set(exc.value.message_dict) == {"health"}
    services.save_sensitive(b, {"passport": "4510 123456"})  # паспорт — без особого согласия
    give_consent(b, kind=ConsentKind.SPECIAL)
    services.save_sensitive(b, {"health": "Диабет 2 типа", "disability": "II группа"})
    sensitive = services.get_sensitive(b)
    assert sensitive.health == "Диабет 2 типа" and sensitive.passport == "4510 123456"


def test_ft_p_03_stored_encrypted_and_audited_without_values(make_beneficiary):
    b = make_beneficiary()
    give_consent(b, kind=ConsentKind.SPECIAL)
    services.save_sensitive(b, {"health": "Диабет 2 типа", "snils": "112-233-445 95"})
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT health, snils FROM beneficiaries_beneficiarysensitive "
            "WHERE beneficiary_id = %s",
            [b.pk],
        )
        raw_health, raw_snils = cursor.fetchone()
        cursor.execute("SELECT health FROM beneficiaries_historicalbeneficiarysensitive")
        raw_history = cursor.fetchone()[0]
    for raw in (raw_health, raw_snils, raw_history):
        assert "Диабет" not in raw and "112" not in raw
    log = AuditLog.objects.get(action=AuditAction.UPDATE, object_id=str(b.pk))
    assert log.fields == ["health", "snils"] and "Диабет" not in str(log.extra)


def test_key_rotation_covers_sensitive_block_and_its_history(make_beneficiary, settings):
    """После ротации и удаления старого ключа читаются и данные, и их история."""
    key_a = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    key_b = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="
    settings.FIELD_ENCRYPTION_KEYS = f"old:{key_a}"
    b = make_beneficiary()
    services.save_sensitive(b, {"passport": "4510 123456"})
    services.save_sensitive(b, {"passport": "4510 654321"})

    settings.FIELD_ENCRYPTION_KEYS = f"new:{key_b},old:{key_a}"
    call_command("rotate_encryption_keys", stdout=io.StringIO())
    settings.FIELD_ENCRYPTION_KEYS = f"new:{key_b}"
    sensitive = services.get_sensitive(b)
    assert sensitive.passport == "4510 654321"
    history = sorted(h.passport for h in sensitive.history.all())
    assert history == ["4510 123456", "4510 654321"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("11223344595", "112-233-445 95"), ("112 233 445 95", "112-233-445 95"), ("", "")],
)
def test_snils_valid(raw, expected):
    assert services.normalize_snils(raw) == expected


@pytest.mark.parametrize("raw", ["112-233-445 96", "123", "1122334459512"])
def test_snils_invalid(raw):
    with pytest.raises(ValidationError):
        services.normalize_snils(raw)


# --- ФТ-П-06: дубли ----------------------------------------------------------------------------


def _new(**fields):
    data = {
        "last_name": "Орлова",
        "first_name": "Вера",
        "birth_date": ADULT_BIRTH,
        "phone": "+79035550011",
    } | fields
    return data


def test_ft_p_06_duplicates(curator):
    services.create_beneficiary(_new(), curator=curator)
    with pytest.raises(services.DuplicateBeneficiaryError) as exc:
        services.create_beneficiary(_new(phone=""), curator=curator)  # ФИО + дата
    assert exc.value.matches[0].reasons == ["ФИО и дата рождения"]
    with pytest.raises(services.DuplicateBeneficiaryError) as exc:
        services.create_beneficiary(
            _new(last_name="Другая", birth_date=datetime.date(1960, 1, 1)), curator=curator
        )
    assert exc.value.matches[0].reasons == ["телефон"]
    services.create_beneficiary(_new(), curator=curator, confirm_duplicates=True)
    assert Beneficiary.objects.count() == 2


def test_ft_p_06_archived_checked(curator):
    b = services.create_beneficiary(_new(), curator=curator)
    services.archive_beneficiary(b)
    with pytest.raises(services.DuplicateBeneficiaryError):
        services.create_beneficiary(_new(), curator=curator)


# --- Кураторы (п. 2.3) -------------------------------------------------------------------------


def test_second_curator_access(make_beneficiary, make_user):
    b = make_beneficiary()
    second = make_user("curator")
    assert not Beneficiary.objects.visible_to(second).exists()
    access = services.grant_access(b, second)
    assert list(Beneficiary.objects.visible_to(second)) == [b]
    with pytest.raises(ValidationError, match="уже есть доступ"):
        services.grant_access(b, second)
    services.revoke_access(access)
    assert not Beneficiary.objects.visible_to(second).exists()
    services.grant_access(b, second)  # можно открыть снова
    actions = AuditLog.objects.filter(
        object_id=str(b.pk), action=AuditAction.PERMISSION_CHANGE
    ).count()
    assert actions == 3


def test_assign_curator_moves_case(make_beneficiary, make_user, curator):
    b = make_beneficiary()
    new = make_user("curator")
    services.assign_curator(b, new)
    assert list(Beneficiary.objects.visible_to(new)) == [b]
    assert not Beneficiary.objects.visible_to(curator).exists()
    assert Notification.objects.filter(recipient=new, kind=NotificationKind.CASE_ASSIGNED).exists()


def test_case_in_work_cannot_lose_curator(make_beneficiary):
    b = to_in_work(make_beneficiary())
    with pytest.raises(ValidationError):
        services.assign_curator(b, None)


# --- ФТ-П-02: связанные лица -------------------------------------------------------------------


def test_ft_p_02_related_persons(make_beneficiary):
    b = make_beneficiary()
    person = services.add_related_person(
        b,
        {
            "full_name": " Орлов  Иван ",
            "relation": "сын",
            "phone": "8 903 000 00 01",
            "is_legal_representative": False,
        },
    )
    assert person.full_name == "Орлов Иван" and person.phone == "+79030000001"
    services.update_related_person(
        person, {"full_name": "Орлов Иван", "relation": "сын", "is_legal_representative": True}
    )
    person.refresh_from_db()
    assert person.is_legal_representative and person.phone == ""
    services.archive_related_person(person)
    assert not RelatedPerson.objects.active().exists()
    with pytest.raises(ValidationError):
        services.add_related_person(b, {"full_name": "", "relation": ""})


def test_archived_card_read_only(make_beneficiary):
    b = make_beneficiary()
    services.archive_beneficiary(b)
    for call in (
        lambda: services.update_beneficiary(b, {"address": "x"}),
        lambda: services.change_status(b, S.ASSESSMENT),
        lambda: services.save_sensitive(b, {"passport": "x"}),
        lambda: services.add_related_person(b, {"full_name": "x", "relation": "y"}),
    ):
        with pytest.raises(ValidationError, match="в архиве"):
            call()
