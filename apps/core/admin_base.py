"""Общая основа классов админ-панели."""

from typing import Any

from django.contrib import admin, messages
from django.db.models import Model, QuerySet
from django.http import HttpRequest
from unfold.admin import ModelAdmin

from apps.accounts.roles import RoleCode, user_has_role
from apps.audit.models import AuditAction
from apps.audit.services import log_event


class CRMModelAdmin(ModelAdmin):
    """Права — по роли «Администратор», а не по встроенным правам Django.

    Физическое удаление выключено (п. 4.1 ТЗ): записи переводятся в архив.
    Каждое создание и изменение пишется в журнал аудита (без значений полей).
    """

    list_filter_submit = True
    actions = ["archive_selected", "restore_selected"]

    def _is_admin(self, request: HttpRequest) -> bool:
        return user_has_role(request.user, RoleCode.ADMIN)

    def has_module_permission(self, request: HttpRequest) -> bool:
        return self._is_admin(request)

    def has_view_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return self._is_admin(request)

    def has_add_permission(self, request: HttpRequest, *args: Any) -> bool:
        return self._is_admin(request)

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return self._is_admin(request)

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    # Встроенный журнал админки (django_admin_log) хранит строковое представление объекта —
    # то есть ФИО — и не обезличивается. Вместо него — журнал аудита и django-simple-history.
    def log_addition(self, request: HttpRequest, obj: Model, message: Any) -> Any:
        return None

    def log_change(self, request: HttpRequest, obj: Model, message: Any) -> Any:
        return None

    def log_deletion(self, request: HttpRequest, obj: Model, object_repr: str) -> Any:
        return None

    def log_deletions(self, request: HttpRequest, queryset: QuerySet) -> Any:
        return None

    def get_actions(self, request: HttpRequest) -> dict[str, Any]:
        actions = super().get_actions(request)
        if not hasattr(self.model, "is_archived"):
            actions.pop("archive_selected", None)
            actions.pop("restore_selected", None)
        return actions

    def save_model(self, request: HttpRequest, obj: Model, form: Any, change: bool) -> None:
        super().save_model(request, obj, form, change)
        log_event(
            AuditAction.UPDATE if change else AuditAction.CREATE,
            request=request,
            obj=obj,
            fields=form.changed_data,
        )

    @admin.action(description="Перевести в архив")
    def archive_selected(self, request: HttpRequest, queryset: QuerySet) -> None:
        count = 0
        for obj in queryset.filter(is_archived=False):
            obj.archive()
            log_event(AuditAction.ARCHIVE, request=request, obj=obj)
            count += 1
        self.message_user(request, f"В архив переведено записей: {count}.", messages.SUCCESS)

    @admin.action(description="Вернуть из архива")
    def restore_selected(self, request: HttpRequest, queryset: QuerySet) -> None:
        count = 0
        for obj in queryset.filter(is_archived=True):
            obj.restore()
            log_event(AuditAction.RESTORE, request=request, obj=obj)
            count += 1
        self.message_user(request, f"Восстановлено записей: {count}.", messages.SUCCESS)
