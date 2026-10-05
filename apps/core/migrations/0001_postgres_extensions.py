from django.contrib.postgres.operations import CITextExtension, TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    """Расширения PostgreSQL: pg_trgm — поиск и проверка дублей по ФИО и телефону (п. 4.4),
    citext — регистронезависимые email и логины."""

    initial = True
    dependencies: list[tuple[str, str]] = []

    operations = [
        TrigramExtension(),
        CITextExtension(),
    ]
