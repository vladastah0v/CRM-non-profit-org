"""Справочники (ФТ-А-02): администратор правит их без участия разработчика."""

from django.contrib import admin

from .admin_base import CRMModelAdmin
from .models import (
    AssistanceType,
    BeneficiaryCategory,
    District,
    LeavingReason,
    Program,
    Skill,
    TrainingType,
)


class ReferenceAdmin(CRMModelAdmin):
    list_display: tuple[str, ...] = ("name", "sort_order", "is_archived", "updated_at")
    list_editable: tuple[str, ...] = ("sort_order",)
    list_filter: tuple[str, ...] = ("is_archived",)
    search_fields: tuple[str, ...] = ("name",)
    readonly_fields: tuple[str, ...] = ("is_archived", "archived_at", "created_at", "updated_at")


@admin.register(Skill)
class SkillAdmin(ReferenceAdmin):
    fields = ("name", "description", "sort_order", *ReferenceAdmin.readonly_fields)


@admin.register(Program)
class ProgramAdmin(ReferenceAdmin):
    list_display = ("name", "grantor", "contract_number", "start_date", "end_date", "is_archived")
    list_editable = ()
    search_fields = ("name", "grantor", "contract_number")
    fields = (
        "name",
        "grantor",
        "contract_number",
        ("start_date", "end_date"),
        "description",
        "sort_order",
        *ReferenceAdmin.readonly_fields,
    )


@admin.register(AssistanceType)
class AssistanceTypeAdmin(ReferenceAdmin):
    list_display = ("name", "unit", "category", "sort_order", "is_archived")
    list_filter = ("is_archived", "category")
    fields = (
        "name",
        "unit",
        "category",
        "description",
        "sort_order",
        *ReferenceAdmin.readonly_fields,
    )


@admin.register(BeneficiaryCategory)
class BeneficiaryCategoryAdmin(ReferenceAdmin):
    fields = ("name", "description", "sort_order", *ReferenceAdmin.readonly_fields)


@admin.register(LeavingReason)
class LeavingReasonAdmin(ReferenceAdmin):
    fields = ("name", "description", "sort_order", *ReferenceAdmin.readonly_fields)


@admin.register(District)
class DistrictAdmin(ReferenceAdmin):
    list_display = ("name", "city", "sort_order", "is_archived")
    list_filter = ("is_archived", "city")
    search_fields = ("name", "city")
    fields = ("name", "city", "description", "sort_order", *ReferenceAdmin.readonly_fields)


@admin.register(TrainingType)
class TrainingTypeAdmin(ReferenceAdmin):
    fields = ("name", "description", "sort_order", *ReferenceAdmin.readonly_fields)
