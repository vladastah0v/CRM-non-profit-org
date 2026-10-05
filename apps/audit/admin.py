"""Просмотр журнала аудита с фильтрами по пользователю, объекту и дате (ФТ-А-03)."""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from unfold.contrib.filters.admin import RangeDateTimeFilter

from apps.core.admin_base import CRMModelAdmin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(CRMModelAdmin):
    list_display = ("created_at", "username", "action", "object_type", "object_id", "ip")
    list_filter = (
        ("created_at", RangeDateTimeFilter),
        "action",
        "object_type",
    )
    search_fields = ("username", "object_id", "ip")
    date_hierarchy = "created_at"
    list_per_page = 100
    actions: list[str] = []
    fields = (
        "created_at",
        "user",
        "username",
        "action",
        "object_type",
        "object_id",
        "fields",
        "ip",
        "user_agent",
        "extra",
    )
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest, *args: Any) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
