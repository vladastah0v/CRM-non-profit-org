"""Журнал аудита для роли приложения crm_app — только SELECT и INSERT (НФТ-ИБ-07).

Роль создаётся на сервере при инициализации базы (docker/postgres/init/10-app-role.sh).
В разработке и в CI её нет — миграция ничего не делает. Повторный запуск безопасен.
"""

from django.db import migrations

SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crm_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON audit_auditlog FROM crm_app;
        GRANT SELECT, INSERT ON audit_auditlog TO crm_app;
    END IF;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0003_server_side_created_at")]

    operations = [migrations.RunSQL(SQL, reverse_sql=migrations.RunSQL.noop)]
