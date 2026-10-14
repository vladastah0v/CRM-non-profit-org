#!/bin/sh
# Выпуск обновления на сервере (п. 10.1 ТЗ). Запускается на сервере из каталога проекта:
#   sh scripts/deploy.sh <тег или коммит>
# Вызывает его GitHub Actions (.github/workflows/deploy.yml) или администратор вручную.
#   1. внеочередная резервная копия базы;
#   2. сборка образов с версией = коммит (прежние образы остаются — для отката);
#   3. миграции до переключения (они должны быть обратно совместимы);
#   4. переключение контейнеров и проверка /healthz/.
# Откат: APP_VERSION=<прежний коммит> docker compose -f docker-compose.yml \
#        -f docker-compose.prod.yml up -d web celery beat
set -eu

REF="${1:?Укажите тег или коммит}"
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') deploy: $*"; }

PREVIOUS="$(git rev-parse --short HEAD)"
log "текущая версия $PREVIOUS, выпускается $REF"
git fetch --tags origin
git checkout --detach "$REF"
APP_VERSION="$(git rev-parse --short HEAD)"
export APP_VERSION

log "внеочередная резервная копия"
$C exec -T backup crm-backup now

log "сборка образов $APP_VERSION"
$C build web bot

log "миграции"
# entrypoint web выполняет migrate (DJANGO_MIGRATE=1) от роли-владельца, затем — `true`
$C run --rm --no-deps -e DJANGO_COLLECTSTATIC=0 web true

log "переключение"
$C up -d --remove-orphans
# Бот — в профиле bot, обычный up его не трогает. Запущенный бот переключается тоже:
# иначе он остался бы на старом образе и старых адресе API и сети
if [ -n "$($C --profile bot ps -q bot)" ]; then
    log "переключение Telegram-бота"
    $C --profile bot up -d bot
fi

DOMAIN="$(grep -E '^DOMAIN=' .env | cut -d= -f2-)"
for _ in $(seq 1 30); do
    if curl -fsS -m 5 "https://$DOMAIN/healthz/" >/dev/null 2>&1; then
        log "готово: версия $APP_VERSION работает"
        exit 0
    fi
    sleep 5
done
log "ОШИБКА: /healthz/ не отвечает. Откат на $PREVIOUS:"
log "  APP_VERSION=$PREVIOUS $C up -d web celery beat"
exit 1
