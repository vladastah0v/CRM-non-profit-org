#!/bin/sh
# Тест восстановления из резервной копии (п. 5.4, приёмочный сценарий 9.2, п. 10.2 —
# раз в квартал). Проверяет весь путь: полная копия WAL-G → запись после копии →
# восстановление в пустой том → проигрывание WAL → данные совпадают.
# Объём: ROWS=5000000 sh scripts/backup/restore-test.sh (по умолчанию 1 000 000 строк).
set -eu

ROWS="${ROWS:-1000000}"
# Одноразовый ключ шифрования копий (64 hex-символа) — только на время теста
RESTORE_TEST_KEY="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
export RESTORE_TEST_KEY
P="docker compose -p crm-restore-test -f scripts/backup/restore-test.yml"
log() { echo "$(date '+%H:%M:%S') $*"; }
# По TCP: временный сервер инициализации образа слушает только сокет — ждём настоящий
sql() { $P exec -T "$1" psql -h 127.0.0.1 -U postgres -tAc "$2"; }
wait_ready() {
    for _ in $(seq 1 120); do
        if [ "$(sql "$1" "SELECT pg_is_in_recovery()" 2>/dev/null)" = "f" ]; then return 0; fi
        sleep 2
    done
    echo "База $1 не поднялась" >&2
    return 1
}

$P --profile backup --profile restore down -v >/dev/null 2>&1 || true
log "S3 и база-источник"
$P up -d s3 >/dev/null
sleep 10
$P up -d --build db >/dev/null
wait_ready db

log "данные: $ROWS строк"
sql db "CREATE TABLE facts AS SELECT g AS id, md5(g::text) AS txt FROM generate_series(1, $ROWS) g" >/dev/null
log "полная копия"
$P --profile backup run --rm -T --no-deps backup crm-backup now

log "запись после копии (должна вернуться из WAL)"
sql db "INSERT INTO facts SELECT g, 'after-backup' FROM generate_series($ROWS + 1, $ROWS + 1000) g" >/dev/null
sql db "SELECT pg_switch_wal()" >/dev/null
for _ in $(seq 1 30); do
    [ "$(sql db "SELECT last_archived_wal IS NOT NULL AND last_archived_time > now() - interval '1 minute' FROM pg_stat_archiver")" = "t" ] && break
    sleep 2
done
EXPECTED="$(sql db "SELECT count(*) || ':' || sum(id) FROM facts")"
log "источник: $EXPECTED"
$P stop db >/dev/null

log "восстановление в пустой том"
START="$(date +%s)"
$P --profile restore run --rm --no-deps restored crm-restore
$P --profile restore up -d restored >/dev/null
wait_ready restored
SECONDS_TOOK=$(( $(date +%s) - START ))
ACTUAL="$(sql restored "SELECT count(*) || ':' || sum(id) FROM facts")"
log "восстановлено: $ACTUAL за ${SECONDS_TOOK} с"

$P --profile backup --profile restore down -v >/dev/null 2>&1 || true
if [ "$EXPECTED" = "$ACTUAL" ]; then
    log "УСПЕХ: данные совпадают, включая записи после копии; время восстановления ${SECONDS_TOOK} с"
else
    log "ОШИБКА: ожидалось $EXPECTED, получено $ACTUAL"
    exit 1
fi
