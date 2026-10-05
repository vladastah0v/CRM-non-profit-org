import uuid
from typing import Any, Self

from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django_otp.models import Device, ThrottlingMixin, TimestampMixin
from django_otp.plugins.otp_totp.models import TOTPDevice, default_key

from apps.core.context import get_current_user
from apps.core.fields import EncryptedTextField
from apps.core.models import BaseModel

from .roles import RoleCode, user_has_role


class Role(BaseModel):
    """Роль из раздела 2.1 ТЗ. Набор ролей фиксирован: от кода зависит логика прав."""

    code = models.CharField("код", max_length=30, choices=RoleCode.choices, unique=True)
    name = models.CharField("название", max_length=100)
    description = models.TextField("описание", blank=True)

    class Meta:
        verbose_name = "роль"
        verbose_name_plural = "роли"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class UserQuerySet(models.QuerySet):
    def visible_to(self, user: Any) -> Self:
        """Пользователи и роли — только администратор (матрица 2.2)."""
        if user_has_role(user, RoleCode.ADMIN):
            return self.all()
        return self.none()


class CRMUserManager(UserManager.from_queryset(UserQuerySet)):  # type: ignore[misc]
    def get_by_natural_key(self, username: str | None) -> "User":
        # Логин не зависит от регистра: «Ivanova» и «ivanova» — один пользователь
        return self.get(username__iexact=username)


class User(AbstractUser):  # type: ignore[django-manager-missing]
    """Сотрудник фонда (или волонтёр с учётной записью). Первичный ключ — UUID (п. 4.1)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    middle_name = models.CharField("отчество", max_length=150, blank=True)
    roles = models.ManyToManyField(
        Role,
        through="UserRole",
        through_fields=("user", "role"),
        related_name="users",
        blank=True,
        verbose_name="роли",
    )

    # Блокировка после неудачных попыток входа (НФТ-ИБ-03). Счётчик попыток — в Redis
    # (apps.accounts.lockout), здесь только срок блокировки: его видит и снимает администратор.
    locked_until = models.DateTimeField("заблокирован до", null=True, blank=True, editable=False)

    # Одноразовый код подключения 2FA. Выдаёт администратор лично или по телефону:
    # без него знание одного пароля не позволяет привязать своё устройство.
    otp_enrollment_code = models.CharField(max_length=255, blank=True, editable=False)
    otp_enrollment_expires = models.DateTimeField(
        "код подключения 2FA действует до", null=True, blank=True, editable=False
    )

    updated_at = models.DateTimeField("изменено", auto_now=True)
    created_by = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, editable=False, related_name="+"
    )
    updated_by = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, editable=False, related_name="+"
    )

    objects = CRMUserManager()  # type: ignore[misc]

    class Meta:
        verbose_name = "пользователь"
        verbose_name_plural = "пользователи"
        ordering = ["last_name", "first_name"]
        constraints = [
            models.UniqueConstraint(
                Lower("username"),
                name="user_username_ci_unique",
                violation_error_message="Пользователь с таким логином уже существует.",
            )
        ]

    def __str__(self) -> str:
        return self.full_name or self.username

    @property
    def full_name(self) -> str:
        parts = (self.last_name, self.first_name, self.middle_name)
        return " ".join(p for p in parts if p)

    def save(self, *args: Any, **kwargs: Any) -> None:
        user = get_current_user()
        if user is not None and user.pk != self.pk:
            if self._state.adding and self.created_by_id is None:
                self.created_by = user
            self.updated_by = user
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = {*kwargs["update_fields"], "updated_by", "updated_at"}
        super().save(*args, **kwargs)

    def is_locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > timezone.now()

    def has_enrollment_code(self) -> bool:
        return bool(
            self.otp_enrollment_code
            and self.otp_enrollment_expires
            and self.otp_enrollment_expires > timezone.now()
        )

    def check_enrollment_code(self, raw: str) -> bool:
        normalized = raw.replace("-", "").replace(" ", "").upper()
        return self.has_enrollment_code() and check_password(normalized, self.otp_enrollment_code)


class SecureTOTPDevice(TimestampMixin, ThrottlingMixin, Device):
    """TOTP-устройство с зашифрованным секретом (НФТ-ИБ-05).

    Стандартный TOTPDevice из django-otp хранит секрет в БД открытым текстом: утечка дампа
    свела бы 2FA к одному паролю. Поля и логика проверки — как у TOTPDevice: методы
    переиспользуются, а не копируются, поэтому исправления django-otp подхватываются.
    """

    key = EncryptedTextField("секрет", default=default_key)
    step = models.PositiveSmallIntegerField(default=30)
    t0 = models.BigIntegerField(default=0)
    digits = models.PositiveSmallIntegerField(choices=[(6, 6), (8, 8)], default=6)
    tolerance = models.PositiveSmallIntegerField(default=1)
    drift = models.SmallIntegerField(default=0)
    last_t = models.BigIntegerField(default=-1)

    bin_key = TOTPDevice.bin_key
    config_url = TOTPDevice.config_url
    verify_token = TOTPDevice.verify_token
    get_throttle_factor = TOTPDevice.get_throttle_factor
    _read_str_from_settings = TOTPDevice._read_str_from_settings

    class Meta(Device.Meta):
        verbose_name = "устройство 2FA"
        verbose_name_plural = "устройства 2FA"


class UserRole(BaseModel):
    """Выданная роль: кто и когда её выдал хранится в created_by / created_at."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="user_roles")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="user_roles")

    class Meta:
        verbose_name = "роль пользователя"
        verbose_name_plural = "роли пользователя"
        constraints = [
            models.UniqueConstraint(fields=["user", "role"], name="user_role_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.user} — {self.role}"
