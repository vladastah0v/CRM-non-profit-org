#!/bin/sh
set -eu

# Миграции и сборка статики выполняются только сервисом web (DJANGO_MIGRATE=1),
# чтобы Celery-процессы не запускали их параллельно.
if [ "${DJANGO_MIGRATE:-0}" = "1" ]; then
    # На сервере миграции идут от роли-владельца таблиц (MIGRATE_DATABASE_URL),
    # а приложение работает от роли без права менять схему и журнал аудита
    DATABASE_URL="${MIGRATE_DATABASE_URL:-$DATABASE_URL}" python manage.py migrate --noinput
fi

if [ "${DJANGO_COLLECTSTATIC:-0}" = "1" ]; then
    python manage.py collectstatic --noinput
fi

exec "$@"
