#!/bin/sh
# Восстановление базы из копии WAL-G в ПУСТОЙ каталог данных (п. 5.4, docs/deploy.md):
#   crm-restore                         — на последний доступный момент (вся история WAL);
#   crm-restore "2026-10-08 14:00:00+03" — на момент времени (например, до ошибки).
# После него обычный запуск PostgreSQL проигрывает WAL и открывает базу на запись.
set -eu

: "${PGDATA:=/var/lib/postgresql/data}"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') restore: $*"; }

if [ -n "$(ls -A "$PGDATA" 2>/dev/null)" ]; then
    echo "Каталог $PGDATA не пуст — восстановление только в пустой том." >&2
    exit 1
fi

log "загрузка последней полной копии"
wal-g backup-fetch "$PGDATA" LATEST
touch "$PGDATA/recovery.signal"
{
    echo "restore_command = 'wal-g wal-fetch %f %p'"
    if [ -n "${1:-}" ]; then
        echo "recovery_target_time = '$1'"
        echo "recovery_target_action = 'promote'"
    fi
} >> "$PGDATA/postgresql.auto.conf"
chown -R postgres:postgres "$PGDATA"
chmod 0700 "$PGDATA"
log "готово: запустите PostgreSQL — он проиграет журналы WAL${1:+ до $1}"
