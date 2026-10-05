from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = "apps.audit"
    label = "audit"
    verbose_name = "Журнал аудита"

    def ready(self) -> None:
        from . import signals  # noqa: F401
