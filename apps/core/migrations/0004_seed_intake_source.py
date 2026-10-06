"""Источник «Сайт фонда» — его ставит анкетам с сайта приём анкет (ФТ-В-03).
Остальные источники администратор добавляет в справочнике."""

from django.db import migrations


def seed(apps, schema_editor):
    VolunteerSource = apps.get_model("core", "VolunteerSource")
    VolunteerSource.objects.get_or_create(
        name="Сайт фонда",
        defaults={"description": "Анкета, отправленная через форму на сайте", "sort_order": 10},
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0003_volunteersource_savedlistfilter")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
