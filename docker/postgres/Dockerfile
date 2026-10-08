# syntax=docker/dockerfile:1.7
# PostgreSQL 16 + WAL-G (п. 5.4 ТЗ, НФТ-ИБ-06): непрерывный архив WAL и ежедневные полные
# копии в S3-хранилище в РФ (другой дата-центр), зашифрованные ключом libsodium.
# Используется на сервере (docker-compose.prod.yml) и в тесте восстановления.
FROM postgres:16-bookworm

ARG WALG_VERSION=v3.0.9
ARG TARGETARCH
# Контрольные суммы архивов релиза: подменённый файл не пройдёт сборку
ARG WALG_SHA256_AMD64=4f03ee4679db7f660bfd1b2b8291ac5df247cd2ec48074b60c112f4897299f24
ARG WALG_SHA256_ARM64=20191a37ad091a880c25f4cc907e6f0deb7f12bdd8a53fd62dbc7fcbf2eac712

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl jq; \
    rm -rf /var/lib/apt/lists/*; \
    case "${TARGETARCH:-amd64}" in \
        arm64) arch=aarch64; sum="$WALG_SHA256_ARM64" ;; \
        *) arch=amd64; sum="$WALG_SHA256_AMD64" ;; \
    esac; \
    curl -fsSL -o /tmp/walg.tar.gz \
        "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/wal-g-pg-22.04-${arch}.tar.gz"; \
    echo "${sum}  /tmp/walg.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/walg.tar.gz -C /tmp; \
    install -m 0755 "/tmp/wal-g-pg-22.04-${arch}" /usr/local/bin/wal-g; \
    rm -f /tmp/walg.tar.gz "/tmp/wal-g-pg-22.04-${arch}"; \
    wal-g --version

COPY --chmod=0755 backup.sh /usr/local/bin/crm-backup
COPY --chmod=0755 restore.sh /usr/local/bin/crm-restore
