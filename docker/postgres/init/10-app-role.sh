#!/bin/sh
# Роль приложения (docs/security.md, НФТ-ИБ-07): читать и менять данные можно, менять схему,
# отключать триггеры и очищать таблицы — нельзя (это может только владелец таблиц).
# Журнал аудита роли приложения — только SELECT и INSERT: миграция audit 0004.
# Выполняется один раз — при создании пустой базы.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v app_password="$POSTGRES_APP_PASSWORD" <<'SQL'
CREATE ROLE crm_app LOGIN PASSWORD :'app_password';
GRANT CONNECT ON DATABASE :"DBNAME" TO crm_app;
GRANT USAGE ON SCHEMA public TO crm_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
-- Таблицы и последовательности, которые миграции создадут от имени владельца
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO crm_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO crm_app;
SQL
