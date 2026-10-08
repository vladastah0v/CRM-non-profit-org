# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# 1. Сборка CSS: Tailwind standalone CLI, без Node.js
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS tailwind
ARG TAILWIND_VERSION=v4.1.11
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN case "${TARGETARCH}" in arm64) arch=arm64 ;; *) arch=x64 ;; esac \
    && curl -fsSL -o /usr/local/bin/tailwindcss \
       "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-${arch}" \
    && chmod +x /usr/local/bin/tailwindcss
WORKDIR /src
COPY static/src ./static/src
COPY static/js ./static/js
COPY templates ./templates
COPY apps ./apps
RUN tailwindcss -i static/src/main.css -o static/dist/main.css --minify

# ---------------------------------------------------------------------------
# 2. Telegram-бот: отдельный образ без Django, ходит только во внутренний API
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS bot
ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PIP_NO_CACHE_DIR=1     PIP_DISABLE_PIP_VERSION_CHECK=1
RUN groupadd --gid 1000 bot && useradd --uid 1000 --gid bot --create-home bot
WORKDIR /app
COPY requirements/bot.txt ./requirements/bot.txt
RUN pip install -r requirements/bot.txt
COPY --chown=bot:bot bot ./bot
USER bot
CMD ["python", "-m", "bot"]

# ---------------------------------------------------------------------------
# 3. Приложение (последняя стадия — цель сборки по умолчанию)
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --create-home app
WORKDIR /app

ARG REQUIREMENTS=requirements/base.txt
COPY requirements ./requirements
RUN pip install -r "${REQUIREMENTS}"

COPY --chown=app:app . .
COPY --from=tailwind --chown=app:app /src/static/dist ./static/dist
RUN sed -i 's/\r$//' docker/entrypoint.sh \
    && mkdir -p /app/staticfiles /app/media \
    && chown app:app /app/staticfiles /app/media

USER app
EXPOSE 8000
ENTRYPOINT ["sh", "/app/docker/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "-c", "docker/gunicorn.conf.py"]
