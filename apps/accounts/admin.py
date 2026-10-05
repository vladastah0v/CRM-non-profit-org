from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from unfold.admin import ModelAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("ФИО и контакты", {"fields": ("last_name", "first_name", "middle_name", "email")}),
        ("Доступ", {"fields": ("is_active", "is_staff", "is_superuser")}),
        ("Даты", {"fields": ("last_login", "date_joined")}),
    )
    list_display = ("username", "full_name", "email", "is_active")
    search_fields = ("username", "last_name", "first_name", "email")
    readonly_fields = ("last_login", "date_joined")
