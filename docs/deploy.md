# Развёртывание, резервные копии и восстановление

Для разработчика или администратора сервера. Требования: п. 5.4, 6.4, 10.1–10.2 ТЗ;
развернуть с нуля — до 2 часов, восстановить из копии — до 4 часов.

## Что нужно заранее

| Что | Зачем |
|---|---|
| Сервер у российского облачного провайдера: 4 vCPU, 8 ГБ, 80 ГБ SSD, Ubuntu 24.04, **шифрованный диск** | Рабочее окружение (НФТ-ПД-01, НФТ-ИБ-06). Тестовому хватит 2 vCPU и 4 ГБ |
| Домен и DNS-запись A на IP сервера | HTTPS (НФТ-ИБ-01) |
| Бакет S3 для файлов в РФ (Yandex Object Storage, Selectel, VK Cloud), с **версионированием** | Сканы, документы, фото — зашифрованы приложением |
| Отдельный бакет S3 для резервных копий **в другом дата-центре** (другой регион или провайдер в РФ) | Копии базы (п. 5.4) |
| SMTP (необязательно), Telegram-бот (docs/bot.md), мониторинг Uptime Kuma | Письма, бот, оповещения |

Все доступы оформляются на фонд, у администратора фонда — доступ к каждому (п. 10.3).

## Сервер с нуля (≈ 1,5 часа)

1. **Доступ только по SSH-ключам** (НФТ-ИБ-09):

   ```bash
   sudo adduser deploy && sudo usermod -aG sudo deploy      # ключ — в ~deploy/.ssh/authorized_keys
   sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/; s/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
   sudo systemctl restart ssh
   ```

2. **Брандмауэр и обновления безопасности** (порты базы и Redis наружу закрыты):

   ```bash
   sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable
   sudo apt update && sudo apt install -y unattended-upgrades git && sudo dpkg-reconfigure -plow unattended-upgrades
   ```

3. **Docker**: `curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker deploy`
   (перелогиниться).
4. **Код**: `git clone <репозиторий фонда> /opt/crm && cd /opt/crm`.
5. **`.env`** — из `.env.example`. Секреты генерируются на сервере и хранятся в менеджере
   паролей фонда, **отдельно от резервных копий**:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(50))"   # DJANGO_SECRET_KEY
   python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # POSTGRES_PASSWORD, POSTGRES_APP_PASSWORD
   python3 -c "import secrets; print(secrets.token_hex(32))"       # BACKUP_ENCRYPTION_KEY, INTAKE_HMAC_SECRET
   docker compose run --rm --no-deps web python manage.py generate_encryption_key   # FIELD_ENCRYPTION_KEYS
   ```

   Обязательно для сервера: `DJANGO_SETTINGS_MODULE=config.settings.prod`, `DOMAIN`,
   `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS=https://<домен>`, нестандартный
   `DJANGO_ADMIN_URL`, `DATABASE_URL` (роль-владелец `crm`) и
   `APP_DATABASE_URL=postgres://crm_app:<POSTGRES_APP_PASSWORD>@db:5432/crm` (роль приложения),
   `S3_*` (файлы), `BACKUP_*` (копии), `PD_RETENTION_DAYS` (срок хранения, утверждает фонд).
6. **Первый сертификат** (до запуска Nginx — порт 80 свободен):

   ```bash
   C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
   $C run --rm -p 80:80 --entrypoint certbot certbot certonly --standalone \
       -d "$DOMAIN" --email <почта фонда> --agree-tos -n
   ```

7. **Запуск**: `$C up -d --build` — база создаётся с ролью приложения `crm_app`, миграции
   выполняются от роли-владельца, приложение работает от `crm_app`.
8. **Первый администратор** и его код 2FA:

   ```bash
   $C exec web python manage.py createsuperuser
   $C exec web python manage.py issue_2fa_code <логин>
   ```

9. **Проверка**: `https://<домен>/healthz/` → `{"status": "ok"}`; вход; в `docker compose logs backup`
   — «расписание: ежедневно в 02:30». Первая копия — сразу: `$C exec backup crm-backup now`.
10. **Мониторинг** (Uptime Kuma): HTTP-проверка `/healthz/` каждую минуту и Push-монитор
    «резервная копия» с интервалом 25 часов — его адрес в `BACKUP_HEARTBEAT_URL`.
11. Справочники, пользователи, роли — по [руководству администратора](user-guide/admin.md).

## Резервные копии (п. 5.4)

| Что | Как |
|---|---|
| Полная копия базы | Ежедневно в `BACKUP_TIME` (по умолчанию 02:30), сервис `backup` (WAL-G) |
| Журналы транзакций (WAL) | Непрерывно, не реже раза в 5 минут — восстановление на любой момент, потеря при сбое ≤ 5 минут (требование — 15) |
| Хранение | 30 ежедневных; копия 1-го числа — ежемесячная, 12 последних |
| Где | Бакет `BACKUP_S3_*` в другом дата-центре в РФ |
| Шифрование | Ключом `BACKUP_ENCRYPTION_KEY` (libsodium) до отправки; плюс шифрование хранилища |
| Контроль | Сигнал в мониторинг после каждой копии; нет сигнала 25 часов — оповещение |
| Файлы (сканы, фото) | В бакете S3 с версионированием; настройте у провайдера репликацию бакета в другой дата-центр. Файлы и так зашифрованы ключом приложения |

Перед каждым выпуском обновления делается внеочередная копия (scripts/deploy.sh).

Список копий: `$C exec backup wal-g backup-list --detail`.

> **Ключи** `FIELD_ENCRYPTION_KEYS` и `BACKUP_ENCRYPTION_KEY` храните отдельно от копий
> (менеджер паролей фонда, у двух ответственных). Без них копия бесполезна.

## Восстановление (≤ 4 часов)

На новом сервере — шаги 1–6 раздела «Сервер с нуля» с **тем же `.env`** (те же ключи), затем:

```bash
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$C build db
$C run --rm --no-deps db crm-restore                      # последняя копия + все WAL
# или на момент до ошибки:  $C run --rm --no-deps db crm-restore "2026-10-08 14:00:00+03"
$C up -d db && $C logs -f db                              # дождаться «database system is ready»
$C up -d                                                  # остальные сервисы
```

Проверить: вход, несколько карточек, последние изменения, `/healthz/`. Файлы восстанавливать
не нужно — они в S3 (при потере бакета — из реплики).

Время: на тестовых данных — см. «Тест восстановления» ниже; на рабочем объёме
(по расчёту — несколько ГБ) — десятки минут. Укладывается в 4 часа с запасом.

## Тест восстановления (раз в квартал, п. 10.2)

```bash
sh scripts/backup/restore-test.sh            # ROWS=5000000 — больший объём
```

Скрипт в изолированном проекте Compose (S3Mock вместо S3) делает полную копию, дописывает
данные после неё, восстанавливает в пустой том и сверяет: строки, записанные после копии,
должны вернуться из WAL. Последний результат — в [acceptance/security-and-tests.md](acceptance/security-and-tests.md).

## Выпуск обновлений (п. 10.1)

- Каждый коммит в `main` проходит CI; после успеха — автоматический выпуск на тестовое
  окружение (`.github/workflows/deploy.yml`).
- На рабочее — кнопкой «Run workflow» → `production`, в нерабочее время фонда. Окружение
  `production` в GitHub защищено подтверждением ответственного.
- На сервере `scripts/deploy.sh`: внеочередная копия → сборка образов с версией → миграции
  (обратно совместимые) → переключение → проверка `/healthz/`.
- **Откат**: `APP_VERSION=<прежний коммит> $C up -d web celery beat` (образы прежних версий
  остаются на сервере). Если новая миграция несовместима — восстановление на момент до выпуска.
- Список изменений для сотрудников — в уведомлении или общем чате, понятными словами.

## Ротация секретов

| Секрет | Как сменить |
|---|---|
| `FIELD_ENCRYPTION_KEYS` | `generate_encryption_key` → новый ключ **первым** в списке → перезапуск → `rotate_encryption_keys` → старый убрать. Старый ключ хранить, пока живы копии базы со старым шифрованием (до 12 месяцев) |
| `BACKUP_ENCRYPTION_KEY` | Новые копии шифруются новым ключом; старый хранить 12 месяцев — им зашифрованы прежние копии |
| `DJANGO_SECRET_KEY` | Заменить и перезапустить: все сессии и коды привязки бота станут недействительными |
| `INTAKE_HMAC_SECRET` | Одновременно на сервере CRM и сайта фонда |
| Токен бота, сервисный токен API | Перевыпуск у @BotFather / `issue_service_token`, старый — в архив (docs/bot.md) |
| Пароли PostgreSQL | `ALTER ROLE ... PASSWORD`, затем `.env` и перезапуск |

## Регламентные работы (п. 10.2)

| Работа | Периодичность | Как |
|---|---|---|
| Проверка резервных копий | Ежедневно, автоматически | Push-монитор `BACKUP_HEARTBEAT_URL` |
| Обновления ОС и зависимостей | Не реже раза в месяц | `unattended-upgrades`; `pip-audit` в CI; пересборка образов (`docker compose build --pull`) |
| Тестовое восстановление | Раз в квартал | `scripts/backup/restore-test.sh`, итог — в протокол |
| Пересмотр пользователей и прав | Раз в квартал | Администратор фонда: админ-панель → Пользователи |
| Обезличивание по сроку хранения | Ежемесячно, автоматически | Celery Beat, отчёт администраторам (`PD_RETENTION_DAYS`) |
| Сертификаты | Автоматически | Сервис `certbot`; контроль раз в квартал |
