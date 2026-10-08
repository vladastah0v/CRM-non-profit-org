"""Виды документов волонтёра из ФТ-В-09. Остальные администратор добавляет в справочнике."""

from django.db import migrations

TYPES = [
    ("Личная медицинская книжка", 10),
    ("Справка об отсутствии судимости", 20),
]


def seed(apps, schema_editor):
    VolunteerDocumentType = apps.get_model("core", "VolunteerDocumentType")
    for name, sort_order in TYPES:
        VolunteerDocumentType.objects.get_or_create(name=name, defaults={"sort_order": sort_order})


class Migration(migrations.Migration):
    dependencies = [("core", "0007_volunteerdocumenttype")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
