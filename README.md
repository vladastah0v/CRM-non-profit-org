# CRM-
CRM система для учета волонтеров и подопечных фонда НКО
Система заменяет таблицы, чаты и бумажные журналы фонда единой базой
волонтёров, подопечных и оказанной помощи с разграничением доступа и
готовыми отчётами для грантодателей.

1.1. Цели проекта
1. Хранить все данные о волонтёрах, подопечных и помощи в одной системе
вместо разрозненных таблиц.
2. Сократить подготовку отчёта по гранту с нескольких дней ручной работы до
выгрузки за несколько минут.
3. Обеспечить, чтобы каждое обращение подопечного имело статус,
ответственного и срок.
4. Вести учёт согласий на обработку персональных данных по каждому
человеку в базе.
5. Ограничить доступ к чувствительным данным только теми сотрудниками,
которым он нужен по работе.
6. Сократить ручную работу координатора: запись на смены и напоминания
волонтёрам — через Telegram-бота.

---

## Стек

Python 3.12 · Django 5.2 LTS · Django REST Framework · PostgreSQL 16 · Redis 7 ·
Celery + Beat · Django Templates + HTMX + Alpine.js (CSP-сборка) · Tailwind CSS v4 ·
Django Admin + Unfold · Nginx + Gunicorn · Docker Compose · GitHub Actions.

## Запуск локально (≈ 15 минут)

Нужен только Docker Desktop (Windows: с WSL 2).

```bash
cp .env.example .env
# В .env: задайте DJANGO_SECRET_KEY, POSTGRES_PASSWORD и тот же пароль в DATABASE_URL,
# DJANGO_SETTINGS_MODULE=config.settings.dev и FIELD_ENCRYPTION_KEYS (команда ниже)
docker compose run --rm --no-deps web python manage.py generate_encryption_key
docker compose up --build
```

| Адрес | Что это |
|---|---|
| http://localhost:8000 | приложение (Django runserver) |
| http://localhost:8080 | то же через Nginx (проверка заголовков и лимитов) |
| http://localhost:8000/admin/ | админ-панель |
| http://localhost:8000/healthz/ | проверка доступности |

Первый администратор:

```bash
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py issue_2fa_code <логин>   # код подключения 2FA
```

Вход — пароль и код из приложения-аутентификатора (Яндекс Ключ, Google Authenticator и т. п.).
Подключить телефон можно только с одноразовым **кодом подключения** от администратора:
одного пароля для этого недостаточно. Остальных сотрудников администратор заводит
в админ-панели, выдаёт роли и действием «Выдать код подключения 2FA» — код, который
передаёт лично или по телефону. То же действие — если сотрудник потерял телефон.

> **Ключ шифрования** (`FIELD_ENCRYPTION_KEYS`) храните отдельно от резервных копий базы.
> Без ключа зашифрованные данные и файлы восстановить нельзя.

## Команды разработчика

```bash
docker compose exec web pytest                   # тесты
docker compose exec web pytest --cov             # тесты с покрытием
docker compose exec web ruff check . && docker compose exec web ruff format --check .
docker compose exec web mypy .
docker compose exec web python manage.py makemigrations
```

Тесты всегда запускаются с настройками `config.settings.test` (флаг `--ds` в `pyproject.toml`).

CSS собирается сервисом `tailwind` при изменении шаблонов. На Windows Docker Desktop
не передаёт контейнеру события об изменении файлов — тогда пересоберите CSS вручную:

```bash
docker compose exec tailwind tailwindcss -i static/src/main.css -o static/dist/main.css
```

## Модули

| Модуль | Адрес | Кто работает |
|---|---|---|
| Волонтёры (ФТ-В-01…08) | `/volunteers/` | координатор, администратор; куратор — только чтение контактов |
| Уведомления (ФТ-У-02) | `/notifications/`, колокольчик в шапке | все сотрудники |
| Приём анкет с сайта (п. 7.1) | `POST /api/v1/intake/volunteer/` | сайт фонда, подпись HMAC — [docs/intake-api.md](docs/intake-api.md) |

Перед работой администратор заполняет справочники: навыки, районы, программы,
типы обучения, причины выбытия, источники прихода.

## Структура

```
config/            настройки (base / dev / test / prod), urls, celery
apps/<модуль>/     models · services (изменения) · selectors (выборки) · views · api · tests
templates/         шаблоны Django
static/src/        исходники Tailwind;  static/js — компоненты Alpine;  static/vendor — HTMX, Alpine
docker/            entrypoint, gunicorn, nginx
docs/              архитектура и решения
```

Правила разработки — в [docs/development.md](docs/development.md).

## Рабочий сервер

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Подробная инструкция по развёртыванию и восстановлению появится на этапе 9 (см. ТЗ, раздел 8).
