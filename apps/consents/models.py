import datetime
import uuid
from typing import Any, Self

from django.apps import apps
from django.db import models
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from simple_history.models import HistoricalRecords

from apps.accounts.roles import RoleCode, user_has_role
from apps.core.fields import EncryptedTextField
from apps.core.models import ArchivableQuerySet, BaseModel


class AttachmentQuerySet(ArchivableQuerySet):
    def visible_to(self, user: Any) -> Self:
        """Администратор видит все файлы, остальные — загруженные ими и файлы карточек,
        к которым у них есть доступ: координатор — сканы согласий и фото волонтёров,
        куратор — сканы согласий и документы своих подопечных."""
        if user_has_role(user, RoleCode.ADMIN):
            return self.all()
        if not getattr(user, "is_authenticated", False):
            return self.none()
        condition = Q(created_by=user)
        if user_has_role(user, RoleCode.COORDINATOR):
            volunteer = apps.get_model("volunteers", "Volunteer")
            volunteer_consents = Consent.objects.filter(
                scan=OuterRef("pk"), volunteer__isnull=False
            )
            volunteer_photos = volunteer.objects.filter(photo=OuterRef("pk"))
            condition |= Q(Exists(volunteer_consents)) | Q(Exists(volunteer_photos))
        if user_has_role(user, RoleCode.CURATOR):
            beneficiary = apps.get_model("beneficiaries", "Beneficiary")
            document = apps.get_model("beneficiaries", "BeneficiaryDocument")
            own = beneficiary.objects.owned_by(user)
            own_consents = Consent.objects.filter(scan=OuterRef("pk"), beneficiary__in=own)
            own_documents = document.objects.filter(file=OuterRef("pk"), beneficiary__in=own)
            condition |= Q(Exists(own_consents)) | Q(Exists(own_documents))
        return self.filter(condition)


class Attachment(BaseModel):
    """Файл: скан согласия, документ, фото. Содержимое хранится зашифрованным (ФТ-Д-05).

    Имя файла тоже шифруется: в нём часто бывает ФИО («Иванова_паспорт.pdf»).
    В хранилище объект лежит под случайным ключом без персональных данных.
    """

    original_name = EncryptedTextField("имя файла")
    content_type = models.CharField("тип", max_length=100)
    size = models.PositiveIntegerField("размер, байт")
    sha256 = models.CharField("хэш SHA-256", max_length=64)
    storage_key = models.CharField("ключ в хранилище", max_length=255, unique=True, editable=False)

    objects = AttachmentQuerySet.as_manager()  # type: ignore[misc]

    class Meta:
        verbose_name = "файл"
        verbose_name_plural = "файлы"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Файл {self.content_type}, {self.size} байт"


# ---------------------------------------------------------------------------
# Согласия на обработку персональных данных (ФТ-Д-01)
# ---------------------------------------------------------------------------
class ConsentKind(models.TextChoices):
    """Виды согласия заданы законом (152-ФЗ), поэтому это перечисление, а не справочник."""

    GENERAL = "general", "На обработку персональных данных"
    SPECIAL = "special", "На обработку особых категорий данных"
    PHOTO_VIDEO = "photo_video", "На фото- и видеосъёмку"


class ConsentSigner(models.TextChoices):
    SUBJECT = "subject", "Сам человек"
    REPRESENTATIVE = "representative", "Законный представитель"


class ConsentChannel(models.TextChoices):
    PAPER = "paper", "Бумажный бланк"
    WEBSITE = "website", "Отметка в форме на сайте"


class ConsentQuerySet(ArchivableQuerySet):
    def visible_to(self, user: Any) -> Self:
        """Матрица 2.2: администратор — все, координатор — согласия волонтёров,
        куратор — согласия своих подопечных. Права нескольких ролей суммируются."""
        if user_has_role(user, RoleCode.ADMIN):
            return self.all()
        condition = Q(pk__in=[])
        if user_has_role(user, RoleCode.COORDINATOR):
            condition |= Q(volunteer__isnull=False)
        if user_has_role(user, RoleCode.CURATOR):
            beneficiary = apps.get_model("beneficiaries", "Beneficiary")
            condition |= Q(beneficiary__in=beneficiary.objects.owned_by(user))
        return self.filter(condition)

    def valid_on(self, day: datetime.date | None = None) -> Self:
        """Действующие на дату: подписаны, не истекли, не отозваны, не в архиве."""
        day = day or timezone.localdate()
        return self.filter(
            Q(valid_until__isnull=True) | Q(valid_until__gte=day),
            Q(revoked_on__isnull=True) | Q(revoked_on__gt=day),
            signed_on__lte=day,
            is_archived=False,
        )


class Consent(BaseModel):
    """Согласие волонтёра или подопечного. Связь с карточкой — двумя внешними ключами,
    заполнен ровно один (п. 4.2 ТЗ)."""

    volunteer = models.ForeignKey(
        "volunteers.Volunteer",
        verbose_name="волонтёр",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="consents",
    )
    beneficiary = models.ForeignKey(
        "beneficiaries.Beneficiary",
        verbose_name="подопечный",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="consents",
    )
    kind = models.CharField("вид согласия", max_length=20, choices=ConsentKind.choices)
    signed_on = models.DateField("дата подписания")
    signer = models.CharField(
        "кто подписал", max_length=20, choices=ConsentSigner.choices, default=ConsentSigner.SUBJECT
    )
    signer_name = models.CharField(
        "ФИО подписавшего",
        max_length=300,
        blank=True,
        help_text="Обязательно, если согласие подписал законный представитель.",
    )
    valid_until = models.DateField(
        "действует до", null=True, blank=True, help_text="Пусто — бессрочно до отзыва."
    )
    channel = models.CharField(
        "как получено", max_length=20, choices=ConsentChannel.choices, default=ConsentChannel.PAPER
    )
    scan = models.ForeignKey(
        Attachment,
        verbose_name="скан",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="consents",
    )
    revoked_on = models.DateField("дата отзыва", null=True, blank=True)
    revocation_note = models.CharField("комментарий к отзыву", max_length=500, blank=True)

    history = HistoricalRecords(
        history_id_field=models.UUIDField(default=uuid.uuid4),
        verbose_name="история согласия",
    )
    objects = ConsentQuerySet.as_manager()  # type: ignore[misc]

    class Meta:
        verbose_name = "согласие"
        verbose_name_plural = "согласия"
        ordering = ["-signed_on", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(volunteer__isnull=False, beneficiary__isnull=True)
                | Q(volunteer__isnull=True, beneficiary__isnull=False),
                name="consent_has_one_subject",
                violation_error_message="Согласие относится ровно к одной карточке.",
            ),
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gte=models.F("signed_on")),
                name="consent_valid_after_signed",
                violation_error_message="Срок действия заканчивается раньше даты подписания.",
            ),
            models.CheckConstraint(
                condition=Q(revoked_on__isnull=True) | Q(revoked_on__gte=models.F("signed_on")),
                name="consent_revoked_after_signed",
                violation_error_message="Дата отзыва раньше даты подписания.",
            ),
            models.CheckConstraint(
                condition=~Q(signer=ConsentSigner.REPRESENTATIVE) | ~Q(signer_name=""),
                name="consent_representative_named",
                violation_error_message="Укажите ФИО законного представителя.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} от {self.signed_on:%d.%m.%Y}"

    def is_valid_on(self, day: datetime.date | None = None) -> bool:
        day = day or timezone.localdate()
        return (
            not self.is_archived
            and self.signed_on <= day
            and (self.valid_until is None or self.valid_until >= day)
            and (self.revoked_on is None or self.revoked_on > day)
        )

    @property
    def state(self) -> str:
        """Состояние для показа в карточке."""
        today = timezone.localdate()
        if self.revoked_on is not None and self.revoked_on <= today:
            return "отозвано"
        if self.valid_until is not None and self.valid_until < today:
            return "истёк срок"
        if self.signed_on > today:
            return "ещё не вступило"
        return "действует"
