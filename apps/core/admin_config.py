from django.contrib.admin.apps import AdminConfig


class CRMAdminConfig(AdminConfig):
    """Подменяет стандартный admin.site на CRMAdminSite (доступ только администратору с 2FA)."""

    default_site = "apps.core.admin_site.CRMAdminSite"
