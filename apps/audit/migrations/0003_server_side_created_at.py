"""Время записи журнала ставит сама база: приложение не может вставить запись «задним
числом» и тем самым сделать её сразу доступной для удаления (срок хранения — 1 год).

Ограничение: роль, владеющая таблицей, может отключить триггеры. Поэтому на рабочем
сервере приложение работает под отдельной ролью без прав владельца (этап 9, docs/deploy).
"""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION audit_log_set_created_at() RETURNS trigger AS $$
BEGIN
    -- clock_timestamp(), а не now(): now() — начало транзакции, а запрос выполняется
    -- в одной транзакции (ATOMIC_REQUESTS), и все его события получили бы одно время
    NEW.created_at := clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_set_created_at
BEFORE INSERT ON audit_auditlog
FOR EACH ROW EXECUTE FUNCTION audit_log_set_created_at();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS audit_log_set_created_at ON audit_auditlog;
DROP FUNCTION IF EXISTS audit_log_set_created_at();
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0002_append_only_trigger")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
