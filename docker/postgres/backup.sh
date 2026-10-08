#!/bin/sh
# Резервное копирование базы (п. 5.4 ТЗ):
#   crm-backup now   — полная копия сейчас (перед деплоем, п. 10.1);
#   crm-backup loop  — ежедневно в BACKUP_TIME (по умолчанию 02:30, часовой пояс TZ).
# После копии: хранить 30 последних ежедневных; копия 1-го числа помечается постоянной,
# постоянных — 12 последних (ежемесячные на год). WAL архивирует сам PostgreSQL
# (archive_command), это даёт восстановление на момент времени.
# При успехе — запрос на BACKUP_HEARTBEAT_URL (Uptime Kuma, healthchecks):
# если сигнала нет сутки, мониторинг оповещает (п. 10.2).
set -eu

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') backup: $*"; }

: "${PGDATA:=/var/lib/postgresql/data}"
: "${BACKUP_TIME:=02:30}"
: "${BACKUP_DAILY_KEEP:=30}"
: "${BACKUP_MONTHLY_KEEP:=12}"

latest_name() {
    wal-g backup-list --json | jq -r 'sort_by(.time) | last | .backup_name'
}

rotate_monthly() {
    # Постоянные (ежемесячные) копии сверх BACKUP_MONTHLY_KEEP снимаются с пометки
    wal-g backup-list --detail --json \
        | jq -r '[.[] | select(.is_permanent)] | sort_by(.time) | reverse | .[].backup_name' \
        | tail -n "+$((BACKUP_MONTHLY_KEEP + 1))" \
        | while read -r name; do
            log "снимаю пометку «постоянная» с $name"
            wal-g backup-mark -i "$name"
        done
}

run_backup() {
    log "полная копия: начало"
    wal-g backup-push "$PGDATA"
    name="$(latest_name)"
    log "полная копия готова: $name"
    if [ "$(date +%d)" = "01" ]; then
        log "ежемесячная копия: $name"
        wal-g backup-mark "$name"
    fi
    rotate_monthly
    wal-g delete retain FULL "$BACKUP_DAILY_KEEP" --confirm
    if [ -n "${BACKUP_HEARTBEAT_URL:-}" ]; then
        curl -fsS -m 30 --retry 3 "$BACKUP_HEARTBEAT_URL" >/dev/null || log "мониторинг недоступен"
    fi
}

seconds_until() {
    now="$(date +%s)"
    target="$(date -d "today $1" +%s)"
    [ "$target" -gt "$now" ] || target="$(date -d "tomorrow $1" +%s)"
    echo $((target - now))
}

case "${1:-}" in
    now)
        run_backup
        ;;
    loop)
        log "расписание: ежедневно в $BACKUP_TIME"
        while :; do
            sleep "$(seconds_until "$BACKUP_TIME")"
            run_backup || log "ОШИБКА: копия не создана — сигнала мониторингу не будет"
        done
        ;;
    *)
        echo "Использование: crm-backup now|loop" >&2
        exit 2
        ;;
esac
