"""Журнал аудита — только добавление (НФТ-ИБ-07).

Запрет действует на уровне PostgreSQL, а не только в коде: изменить запись нельзя
даже прямым SQL-запросом. Удалить можно только записи старше срока хранения (1 год).
"""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION audit_log_protect() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'Журнал аудита нельзя изменять';
    END IF;
    IF TG_OP = 'DELETE' AND OLD.created_at > now() - interval '1 year' THEN
        RAISE EXCEPTION 'Записи журнала аудита хранятся не менее 1 года';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_protect
BEFORE UPDATE OR DELETE ON audit_auditlog
FOR EACH ROW EXECUTE FUNCTION audit_log_protect();

CREATE OR REPLACE FUNCTION audit_log_no_truncate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Журнал аудита нельзя очищать';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_truncate
BEFORE TRUNCATE ON audit_auditlog
FOR EACH STATEMENT EXECUTE FUNCTION audit_log_no_truncate();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_auditlog;
DROP FUNCTION IF EXISTS audit_log_no_truncate();
DROP TRIGGER IF EXISTS audit_log_protect ON audit_auditlog;
DROP FUNCTION IF EXISTS audit_log_protect();
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
