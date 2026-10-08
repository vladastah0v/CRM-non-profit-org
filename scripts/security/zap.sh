#!/bin/sh
# Проверка OWASP ZAP (раздел 9.1 ТЗ; этап 9 и раз в квартал).
# Базовое (пассивное) сканирование всех страниц под сессией администратора — через Nginx
# с рабочими заголовками безопасности, на отдельной базе crm_e2e с тестовыми данными.
# Активные атаки (zap-full-scan) — только на тестовом сервере, по согласованию.
# Отчёт — tmp/zap-report.html. Запуск из корня репозитория при `docker compose up`.
set -eu

ROOT="$PWD"
if command -v cygpath >/dev/null 2>&1; then
    ROOT="$(cygpath -m "$PWD")"
    export MSYS_NO_PATHCONV=1
fi
mkdir -p tmp
chmod 777 tmp 2>/dev/null || true
E2E_ENV="-e DJANGO_SETTINGS_MODULE=config.settings.dev -e DJANGO_MIGRATE=0 -e DJANGO_ALLOWED_HOSTS=crm-nginx-e2e,crm-web-e2e"
in_e2e() {
    # shellcheck disable=SC2086
    docker compose run --rm --no-deps $E2E_ENV web sh -c "export DATABASE_URL=\${DATABASE_URL%/*}/crm_e2e; $1"
}
cleanup() {
    docker logs crm-web-e2e > tmp/zap-web.log 2>&1 || true  # журнал приложения — для разбора ошибок
    docker rm -f crm-web-e2e crm-nginx-e2e >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== Тестовая база crm_e2e"
docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" --if-exists crm_e2e && createdb -U "$POSTGRES_USER" crm_e2e'
in_e2e "python manage.py migrate --noinput -v0 && python manage.py prepare_e2e"
SESSION="$(in_e2e "python manage.py shell -c \"
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.cached_db import SessionStore
from django_otp import DEVICE_ID_SESSION_KEY
from apps.accounts.models import SecureTOTPDevice, User
u = User.objects.get(username='e2e-admin-0'); s = SessionStore()
s[SESSION_KEY] = str(u.pk); s[BACKEND_SESSION_KEY] = 'django.contrib.auth.backends.ModelBackend'
s[HASH_SESSION_KEY] = u.get_session_auth_hash()
s[DEVICE_ID_SESSION_KEY] = SecureTOTPDevice.objects.get(user=u).persistent_id
s.set_expiry(7200); s.save(); print('SESSION=' + s.session_key)
\"" | grep '^SESSION=' | cut -d= -f2)"

echo "== Приложение и Nginx"
cleanup
# shellcheck disable=SC2086
docker compose run -d --no-deps --name crm-web-e2e $E2E_ENV web sh -c \
    'export DATABASE_URL=${DATABASE_URL%/*}/crm_e2e; exec python manage.py runserver 0.0.0.0:8000'
NETWORK="$(docker inspect crm-web-e2e --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | cut -d' ' -f1)"
# Выход (свой и админ-панели) закрыт только на стенде сканирования: иначе паук ZAP нажмёт «Выйти» и дальше
# проверит приложение без сессии
sed -e 's/server web:8000;/server crm-web-e2e:8000;/' \
    -e 's|^    location / {|    location = /accounts/logout/ { return 403; }\n    location = /admin/logout/ { return 403; }\n    location / {|' \
    docker/nginx/dev.conf > tmp/e2e-nginx.conf
docker run -d --name crm-nginx-e2e --network "$NETWORK" \
    -v "$ROOT/tmp/e2e-nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
    -v "$ROOT/docker/nginx/snippets:/etc/nginx/snippets:ro" nginx:1.27-alpine >/dev/null
sleep 8

echo "== OWASP ZAP (baseline)"
# Сессия администратора — заголовком Cookie. Паук отправляет и формы (архив, отзыв, обезличивание) —
# поэтому только на отдельной тестовой базе crm_e2e
docker run --rm --network "$NETWORK" -v "$ROOT/tmp:/zap/wrk" ghcr.io/zaproxy/zaproxy:stable \
    zap-baseline.py -t http://crm-nginx-e2e/ -m 5 -r zap-report.html -J zap-report.json -I \
    -z "-config replacer.full_list(0).description=session \
        -config replacer.full_list(0).enabled=true \
        -config replacer.full_list(0).matchtype=REQ_HEADER \
        -config replacer.full_list(0).matchstr=Cookie \
        -config replacer.full_list(0).regex=false \
        -config replacer.full_list(0).replacement=sessionid=$SESSION" || true
echo "== Отчёт: tmp/zap-report.html"
