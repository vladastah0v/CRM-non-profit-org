#!/bin/sh
# Сквозные тесты в браузере (раздел 9.1 ТЗ: Playwright, перед каждым релизом).
#   1) отдельная база crm_e2e с тестовыми сотрудниками и данными;
#   2) приложение + Nginx с рабочими заголовками безопасности (CSP);
#   3) Chromium в контейнере Playwright; снимки экранов — tmp/e2e/.
# Запуск из корня репозитория при запущенном `docker compose up` (нужны db и redis).
set -eu

ROOT="$PWD"
if command -v cygpath >/dev/null 2>&1; then  # Git Bash на Windows
    ROOT="$(cygpath -m "$PWD")"
    export MSYS_NO_PATHCONV=1
fi
mkdir -p tmp/e2e
# Версия пакета playwright должна совпадать с образом: в нём браузеры этой версии
PLAYWRIGHT_IMAGE="mcr.microsoft.com/playwright/python:v1.49.1-noble"
E2E_ENV="-e DJANGO_SETTINGS_MODULE=config.settings.dev -e DJANGO_MIGRATE=0 -e DJANGO_ALLOWED_HOSTS=crm-nginx-e2e,crm-web-e2e"
in_e2e() {
    # shellcheck disable=SC2086
    docker compose run --rm --no-deps $E2E_ENV web sh -c "export DATABASE_URL=\${DATABASE_URL%/*}/crm_e2e; $1"
}
cleanup() { docker rm -f crm-web-e2e crm-nginx-e2e >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== База crm_e2e"
docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" --if-exists crm_e2e && createdb -U "$POSTGRES_USER" crm_e2e'
in_e2e "python manage.py migrate --noinput -v0 && python manage.py prepare_e2e"

echo "== Приложение и Nginx"
cleanup
# shellcheck disable=SC2086
docker compose run -d --no-deps --name crm-web-e2e $E2E_ENV web sh -c \
    'export DATABASE_URL=${DATABASE_URL%/*}/crm_e2e; exec python manage.py runserver 0.0.0.0:8000'
NETWORK="$(docker inspect crm-web-e2e --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | cut -d' ' -f1)"
# Все тесты входят с одного IP подряд: запас лимита входов (burst) увеличен только для стенда,
# сам лимит (10 в минуту) — как в рабочей конфигурации
sed -e 's/server web:8000;/server crm-web-e2e:8000;/' -e 's/zone=login burst=10/zone=login burst=100/' \
    docker/nginx/dev.conf > tmp/e2e-nginx.conf
docker run -d --name crm-nginx-e2e --network "$NETWORK" \
    -v "$ROOT/tmp/e2e-nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
    -v "$ROOT/docker/nginx/snippets:/etc/nginx/snippets:ro" nginx:1.27-alpine >/dev/null
sleep 8

echo "== Playwright"
docker run --rm --network "$NETWORK" --ipc=host \
    -v "$ROOT/e2e:/e2e:ro" -v "$ROOT/tmp:/mnt/out" \
    -e E2E_BASE_URL=http://crm-nginx-e2e -e E2E_SCREENSHOTS=/mnt/out/e2e \
    -w /e2e "$PLAYWRIGHT_IMAGE" sh -c \
    "pip install -q --disable-pip-version-check playwright==1.49.1 pytest==8.3.4 pytest-playwright==0.6.2 && \
     python -m pytest -c /e2e/pytest.ini -p no:cacheprovider /e2e $*"
echo "== Снимки экранов 360 px — tmp/e2e/"
