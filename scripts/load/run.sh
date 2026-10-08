#!/bin/sh
# Нагрузочная проверка (этап 9, п. 5.3 ТЗ) на отдельной базе crm_load:
#   1) пересоздать базу crm_load и наполнить её расчётным объёмом;
#   2) поднять приложение под Gunicorn с DEBUG=False (config.settings.load);
#   3) 50 одновременных сотрудников в Locust; итоги — tmp/load_stats.csv.
# Запуск из корня репозитория при запущенном `docker compose up` (нужны db и redis).
# Длительность и пользователи: DURATION=5m USERS=50 sh scripts/load/run.sh
# Повторный замер без пересоздания данных: SKIP_DATA=1 sh scripts/load/run.sh
set -eu

DURATION="${DURATION:-3m}"
USERS="${USERS:-50}"
ROOT="$PWD"
if command -v cygpath >/dev/null 2>&1; then  # Git Bash на Windows
    ROOT="$(cygpath -m "$PWD")"
    export MSYS_NO_PATHCONV=1
fi
mkdir -p tmp

# Воркеры Gunicorn — как на рабочем сервере 4 vCPU (2 × CPU + 1)
# DJANGO_MIGRATE=0: точка входа не трогает обычную базу — миграции crm_load идут явно ниже
LOAD_ENV="-e DJANGO_SETTINGS_MODULE=config.settings.load -e DJANGO_MIGRATE=0 -e GUNICORN_WORKERS=${GUNICORN_WORKERS:-9}"
in_load() {
    # shellcheck disable=SC2086
    docker compose run --rm --no-deps $LOAD_ENV web sh -c "export DATABASE_URL=\${DATABASE_URL%/*}/crm_load; $1"
}

if [ "${SKIP_DATA:-0}" != "1" ]; then  # SKIP_DATA=1 — повторный замер на тех же данных
    echo "== База crm_load"
    docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" --if-exists crm_load && createdb -U "$POSTGRES_USER" crm_load'
    in_load "python manage.py migrate --noinput -v0"

    echo "== Данные расчётного объёма (несколько минут)"
    in_load "python manage.py generate_load_data --sessions /app/tmp/load-sessions.json ${GENERATE_ARGS:-}"
fi

echo "== Приложение под Gunicorn"
docker rm -f crm-web-load >/dev/null 2>&1 || true
# shellcheck disable=SC2086
docker compose run -d --no-deps --name crm-web-load $LOAD_ENV web sh -c \
    'export DATABASE_URL=${DATABASE_URL%/*}/crm_load; exec gunicorn config.wsgi:application -c docker/gunicorn.conf.py'
sleep 10
echo "== Locust: $USERS пользователей, $DURATION"
NETWORK="$(docker inspect crm-web-load --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | cut -d' ' -f1)"
locust() {
    docker run --rm --network "$NETWORK" \
        -v "$ROOT/scripts/load:/mnt/locust:ro" -v "$ROOT/tmp:/mnt/out" \
        -e LOAD_SESSIONS=/mnt/out/load-sessions.json \
        locustio/locust:2.32.4 -f /mnt/locust/locustfile.py --headless \
        -u "$USERS" -r 10 --host http://crm-web-load:8000 --only-summary "$@" || true
}
# Прогрев: первые запросы каждого воркера читают и компилируют шаблоны — в итоги не идут
echo "(прогрев ${WARMUP:-60s})"
locust -t "${WARMUP:-60s}" >/dev/null 2>&1
locust -t "$DURATION" --csv /mnt/out/load

docker rm -f crm-web-load >/dev/null
echo "== Итоги: tmp/load_stats.csv (время в мс: медиана, 95-й процентиль)"
