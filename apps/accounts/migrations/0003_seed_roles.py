"""Пять ролей из раздела 2.1 ТЗ."""

from django.db import migrations

ROLES = [
    (
        "admin",
        "Администратор",
        "Пользователи, роли, справочники, настройки, журнал аудита, удаление данных по запросу.",
    ),
    (
        "coordinator",
        "Координатор волонтёров",
        "Анкеты, собеседования, обучение, смены, назначения, учёт часов, рассылки.",
    ),
    (
        "curator",
        "Куратор",
        "Карточки своих подопечных, заявки, оказанная помощь, взаимодействия, документы.",
    ),
    (
        "analyst",
        "Аналитик / отчётность",
        "Отчёты и дашборды по агрегированным данным, выгрузки без лишних ПДн.",
    ),
    (
        "volunteer",
        "Волонтёр",
        "Через Telegram-бота: свои смены, запись, отметка часов, минимальные данные о визите.",
    ),
]


def seed(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    for code, name, description in ROLES:
        Role.objects.update_or_create(code=code, defaults={"name": name, "description": description})


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_roles_lockout")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
