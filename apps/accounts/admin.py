"""Пользователи и роли (ФТ-А-01): создание, роли, блокировка доступа, выдача кода 2FA."""

from contextlib import suppress
from typing import Any

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.exceptions import NotRegistered
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django_otp.plugins.otp_totp.models import TOTPDevice
from unfold.admin import TabularInline
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from apps.audit.models import AuditAction
from apps.audit.services import log_event
from apps.core.admin_base import CRMModelAdmin

from . import lockout, services
from .models import Role, SecureTOTPDevice, ServiceToken, User, UserRole
from .roles import RoleCode, invalidate_role_cache, user_has_role

_ROLES_BEFORE = "_audit_roles_before"


class UserRoleInline(TabularInline):
    model = UserRole
    fk_name = "user"
    extra = 0
    fields = ("role", "created_at", "created_by")
    readonly_fields = ("created_at", "created_by")
    verbose_name = "роль"
    verbose_name_plural = "роли"

    def _is_admin(self, request: HttpRequest) -> bool:
        return user_has_role(request.user, RoleCode.ADMIN)

    def has_view_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return self._is_admin(request)

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return self._is_admin(request)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return self._is_admin(request)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # Снять роль — это удалить связь, а не данные человека
        return self._is_admin(request)


@admin.register(User)
class UserAdmin(BaseUserAdmin, CRMModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm
    inlines = [UserRoleInline]
    actions = ["block_access", "unblock_access", "issue_2fa_code", "unlock_login"]

    list_display = ("username", "full_name", "email", "roles_list", "is_active", "last_login")
    list_filter = ("is_active", "roles")
    search_fields = ("username", "last_name", "first_name", "email")
    ordering = ("last_name", "first_name")
    readonly_fields = ("last_login", "date_joined", "locked_until", "otp_enrollment_expires")
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("ФИО и контакты", {"fields": ("last_name", "first_name", "middle_name", "email")}),
        ("Доступ", {"fields": ("is_active", "locked_until", "otp_enrollment_expires")}),
        ("Даты", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "last_name",
                    "first_name",
                    "middle_name",
                    "email",
                    "password1",
                    "password2",
                ),
            },
        ),
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        return super().get_queryset(request).prefetch_related("roles")

    @admin.display(description="роли")
    def roles_list(self, obj: User) -> str:
        return ", ".join(role.name for role in obj.roles.all()) or "—"

    def get_inlines(self, request: HttpRequest, obj: Any) -> Any:
        # При создании роли выдаются после сохранения учётной записи
        return [] if obj is None else super().get_inlines(request, obj)

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        if change:
            setattr(request, _ROLES_BEFORE, set(obj.roles.values_list("code", flat=True)))
        super().save_model(request, obj, form, change)
        if change and "is_active" in form.changed_data:
            action = AuditAction.USER_UNBLOCKED if obj.is_active else AuditAction.USER_BLOCKED
            log_event(action, request=request, obj=obj)

    def save_related(self, request: HttpRequest, form: Any, formsets: Any, change: bool) -> None:
        super().save_related(request, form, formsets, change)
        user = form.instance
        invalidate_role_cache(user)
        before: set[str] = getattr(request, _ROLES_BEFORE, set())
        after = set(user.roles.values_list("code", flat=True))
        if before != after:
            log_event(
                AuditAction.PERMISSION_CHANGE,
                request=request,
                obj=user,
                fields=["roles"],
                extra={"added": sorted(after - before), "removed": sorted(before - after)},
            )

    def user_change_password(
        self, request: HttpRequest, id: str, form_url: str = ""
    ) -> HttpResponse:
        response = super().user_change_password(request, id, form_url)
        if request.method == "POST" and response.status_code == 302:
            log_event(
                AuditAction.PASSWORD_CHANGED,
                request=request,
                object_type="accounts.user",
                object_id=id,
            )
        return response

    @admin.action(description="Заблокировать доступ (увольнение)")
    def block_access(self, request: HttpRequest, queryset: QuerySet) -> None:
        count = 0
        for user in queryset.filter(is_active=True).exclude(pk=request.user.pk):
            user.is_active = False
            user.save(update_fields=["is_active", "updated_at", "updated_by"])
            log_event(AuditAction.USER_BLOCKED, request=request, obj=user)
            count += 1
        self.message_user(request, f"Заблокировано пользователей: {count}.", messages.SUCCESS)

    @admin.action(description="Разблокировать доступ")
    def unblock_access(self, request: HttpRequest, queryset: QuerySet) -> None:
        count = 0
        for user in queryset.filter(is_active=False):
            user.is_active = True
            user.save(update_fields=["is_active", "updated_at", "updated_by"])
            log_event(AuditAction.USER_UNBLOCKED, request=request, obj=user)
            count += 1
        self.message_user(request, f"Разблокировано пользователей: {count}.", messages.SUCCESS)

    @admin.action(description="Выдать код подключения 2FA (новый сотрудник, потерян телефон)")
    def issue_2fa_code(self, request: HttpRequest, queryset: QuerySet) -> None:
        hours = settings.OTP_ENROLLMENT_TTL_HOURS
        for user in queryset.filter(is_active=True):
            code = services.issue_enrollment_code(request, user)
            self.message_user(
                request,
                f"Код подключения 2FA для «{user.username}»: {code}. Действует {hours} ч. "
                "Передайте его лично или по телефону — не по почте и не в мессенджере. "
                "Код показывается один раз.",
                messages.WARNING,
            )

    @admin.action(description="Снять блокировку после неудачных попыток входа")
    def unlock_login(self, request: HttpRequest, queryset: QuerySet) -> None:
        for user in queryset:
            lockout.reset(user.username, user)
            log_event(AuditAction.USER_UNBLOCKED, request=request, obj=user, extra={"login": 1})
        self.message_user(request, "Блокировка входа снята.", messages.SUCCESS)


# Стандартная админка TOTP из django-otp показала бы секреты устройств — убираем
for _model in (TOTPDevice, SecureTOTPDevice):
    with suppress(NotRegistered):
        admin.site.unregister(_model)


@admin.register(Role)
class RoleAdmin(CRMModelAdmin):
    """Набор ролей фиксирован (от кода зависит логика прав) — можно менять только описание."""

    list_display = ("name", "code", "description")
    fields = ("code", "name", "description")
    readonly_fields = ("code",)
    actions: list[str] = []

    def has_add_permission(self, request: HttpRequest, *args: Any) -> bool:
        return False


@admin.register(ServiceToken)
class ServiceTokenAdmin(CRMModelAdmin):
    """Токены выпускает команда `issue_service_token` (токен виден один раз).
    Здесь — просмотр и отзыв: «Перевести в архив»."""

    list_display = ("name", "key_prefix", "scopes", "expires_at", "last_used_at", "is_archived")
    list_filter = ("is_archived",)
    fields = ("name", "scopes", "key_prefix", "expires_at", "last_used_at", "is_archived")
    readonly_fields = ("name", "scopes", "key_prefix", "last_used_at", "is_archived")
    actions = ["archive_selected"]

    def has_add_permission(self, request: HttpRequest, *args: Any) -> bool:
        return False
