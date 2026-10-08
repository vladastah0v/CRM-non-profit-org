"""ФТ-А-05, п. 7.3: импорт из XLSX и CSV с предпросмотром и отчётом об ошибках."""

import datetime
import io
import zipfile
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from apps.beneficiaries.models import Beneficiary, CaseStatus
from apps.consents.models import Attachment
from apps.core.tests.markup import check_markup
from apps.imports import importers, reader, services
from apps.imports.models import ImportJob, ImportStatus
from apps.notifications.models import Notification, NotificationKind
from apps.requests.models import Assistance
from apps.volunteers.models import Volunteer, VolunteerStatus

from .conftest import csv_file, xlsx

pytestmark = pytest.mark.django_db
VOLUNTEER_HEADER = [
    "Фамилия*", "Имя*", "Отчество", "Дата рождения*", "Телефон*", "Email", "Район", "Навыки",
    "Дата согласия", "Обучение", "Дата обучения", "Статус",
]  # fmt: skip


@pytest.fixture
def admin(make_user):
    return make_user("admin")


@pytest.fixture
def admin_client(verified_client, admin):
    return verified_client(admin)


def upload_and_check(client, kind, file, capture):
    with capture(execute=True):
        response = client.post(reverse("imports:list"), {"kind": kind, "file": file})
    assert response.status_code == 302, response.content.decode()[:500]
    return ImportJob.objects.get()


def test_template_has_required_marks_and_help(admin_client):
    response = admin_client.get(reverse("imports:template", args=["volunteers"]))
    workbook = load_workbook(io.BytesIO(response.content))
    data, help_sheet = workbook.worksheets
    header = [c.value for c in data[1]]
    assert header[:5] == ["Фамилия*", "Имя*", "Отчество", "Дата рождения*", "Телефон*"]
    assert help_sheet.title == "Пояснения"
    assert admin_client.get(reverse("imports:template", args=["users"])).status_code == 404


def test_fta05_volunteers_preview_then_import(
    admin_client, admin, make_volunteer, refs, django_capture_on_commit_callbacks
):
    existing = make_volunteer(phone="+79161110000")
    rows = [
        ("Иванова", "Мария", "", "01.02.1990", "8 916 000-00-01", "", "центральный",
         "вождение; Первая помощь", "01.09.2026", "Вводный инструктаж", "02.09.2026", "Активен"),
        (existing.last_name, existing.first_name, existing.middle_name, existing.birth_date,
         "+7 916 111-00-00", "new@example.org", "", "", "", "", "", ""),
        ("Сидорова", "Ольга", "", "32.13.1990", "89160000003", "", "", "", "", "", "", ""),
        ("Кузнецова", "Анна", "", "05.05.1985", "89160000004", "", "Неизвестный", "", "", "",
         "", ""),
        (existing.last_name, existing.first_name, existing.middle_name, existing.birth_date,
         "89160000005", "", "", "", "", "", "", ""),
    ]  # fmt: skip
    job = upload_and_check(
        admin_client, "volunteers", xlsx(VOLUNTEER_HEADER, rows), django_capture_on_commit_callbacks
    )
    assert job.status == ImportStatus.READY
    assert job.preview == {
        "rows": 5, "create": 2, "update": 1, "skipped": 0, "duplicates": 1, "errors": 2
    }  # fmt: skip
    assert Volunteer.objects.count() == 1  # предпросмотр ничего не сохранил
    assert {(e["row"], e["column"]) for e in job.errors} == {(4, "Дата рождения"), (5, "Район")}
    assert job.duplicates[0]["row"] == 6 and job.duplicates[0]["url"] == existing.get_absolute_url()
    page = admin_client.get(job.get_absolute_url())
    check_markup(page.content.decode())
    assert "Будет создано" in page.content.decode()
    assert Notification.objects.filter(kind=NotificationKind.IMPORT_FINISHED).count() == 1

    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(reverse("imports:confirm", args=[job.pk]))
    job.refresh_from_db()
    assert job.status == ImportStatus.DONE and job.result["create"] == 2
    assert Volunteer.objects.count() == 3
    maria = Volunteer.objects.get(last_name="Иванова")
    assert maria.status == VolunteerStatus.ACTIVE and maria.created_by == admin
    assert maria.district == refs["district"] and maria.skills.count() == 2
    existing.refresh_from_db()
    assert existing.email == "new@example.org"
    assert Volunteer.objects.get(phone="+79160000005").is_possible_duplicate
    # Файл удалён, отчёт об ошибках доступен
    assert job.file is None and not Attachment.objects.exists()
    report = load_workbook(
        io.BytesIO(admin_client.get(reverse("imports:errors", args=[job.pk])).content)
    )
    assert [r[0] for r in report.active.iter_rows(min_row=4, values_only=True)] == [4, 5, 6]


def test_beneficiaries_csv_cp1251(admin_client, curator, brefs, django_capture_on_commit_callbacks):
    text = (
        "Фамилия;Имя;Отчество;Дата рождения;Телефон;Категория;Куратор;Дата согласия;Статус\n"
        f"Петров;Иван;;12.03.1945;89031112233;пожилые одинокие;{curator.username};"
        "01.10.2026;В работе\n"
        "Малышев;Коля;;01.01.2020;;;;01.10.2026;\n"
        "Сидоров;Пётр;;12.03.1950;;;;;Закрыт\n"
    )
    job = upload_and_check(
        admin_client, "beneficiaries", csv_file(text), django_capture_on_commit_callbacks
    )
    assert job.file_format == "csv"
    assert (job.preview["create"], job.preview["errors"]) == (1, 2)
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(reverse("imports:confirm", args=[job.pk]))
    b = Beneficiary.objects.get()
    assert b.status == CaseStatus.IN_WORK and b.curator == curator
    assert b.category == brefs["category"]


def test_assistance_history_idempotent(admin, make_beneficiary, make_active, food, brefs):
    beneficiary = make_beneficiary()
    volunteer = make_active(phone="+79165554433")
    header = ["Фамилия подопечного", "Имя подопечного", "Отчество подопечного",
              "Дата рождения подопечного", "Тип помощи", "Дата", "Количество", "Сумма",
              "Программа", "Исполнитель"]  # fmt: skip
    rows = [
        (beneficiary.last_name, "Пётр", "Ильич", beneficiary.birth_date, "продуктовый набор",
         "15.07.2026", 2, None, brefs["program"].name, "8 916 555-44-33"),
        (beneficiary.last_name, "Пётр", "Ильич", beneficiary.birth_date, "Продуктовый набор",
         "16.07.2026", None, "1 500,50", "", ""),
        ("Нетакого", "Человека", "", "01.01.1950", "Продуктовый набор", "16.07.2026", 1, None,
         "", ""),
        (beneficiary.last_name, "Пётр", "Ильич", beneficiary.birth_date, "Продуктовый набор",
         "16.07.2026", None, None, "", ""),
    ]  # fmt: skip
    table = reader.read_table(xlsx(header, rows).read(), reader.XLSX)
    result = importers.run(importers.AssistanceImporter(admin), table)
    assert result.summary["create"] == 2 and result.summary["errors"] == 2
    facts = {a.provided_on: a for a in Assistance.objects.all()}
    first, second = facts[datetime.date(2026, 7, 15)], facts[datetime.date(2026, 7, 16)]
    assert first.executor_volunteer == volunteer and first.unit == "шт."
    assert second.amount == Decimal("1500.50") and second.executor_user == admin
    again = importers.run(importers.AssistanceImporter(admin), table)
    assert again.summary["create"] == 0 and again.summary["skipped"] == 2  # повтор не задваивает


def test_acceptance_1000_rows_with_error_report(admin):
    """Сценарий 9.2: импорт тестового файла из 1000 строк проходит с отчётом об ошибках."""
    birth = datetime.date(1990, 1, 1)
    rows = []
    for i in range(1000):
        date = "не дата" if i % 100 == 0 else birth + datetime.timedelta(days=i)
        rows.append((f"Тестова{i}", "Анна", "", date, f"+7917{i:07d}"))
    table = reader.read_table(
        xlsx(["Фамилия", "Имя", "Отчество", "Дата рождения", "Телефон"], rows).read(), reader.XLSX
    )
    result = importers.run(importers.VolunteerImporter(admin), table)
    assert result.summary["create"] == 990 and result.summary["errors"] == 10
    assert [e["row"] for e in result.errors] == [i + 2 for i in range(0, 1000, 100)]
    assert Volunteer.objects.count() == 990


def test_missing_columns_and_broken_files(admin, admin_client, django_capture_on_commit_callbacks):
    table = reader.read_table(xlsx(["Фамилия", "Имя"], [("А", "Б")]).read(), reader.XLSX)
    result = importers.run(importers.VolunteerImporter(admin), table)
    assert "Дата рождения" in result.errors[0]["message"]
    broken = SimpleUploadedFile("x.xlsx", b"PK\x03\x04garbage")
    job = upload_and_check(admin_client, "volunteers", broken, django_capture_on_commit_callbacks)
    assert job.status == ImportStatus.FAILED and job.failure and job.file is None
    with pytest.raises(ValidationError):
        reader.detect_format("data.pdf", b"%PDF")


def test_zip_bomb_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"0" * (reader.MAX_UNPACKED + 1))
    with pytest.raises(ValidationError, match="распаковки"):
        reader.read_xlsx(buffer.getvalue())


def test_cancel_deletes_file_and_access(
    admin_client, verified_client, coordinator, django_capture_on_commit_callbacks
):
    job = upload_and_check(
        admin_client,
        "volunteers",
        xlsx(VOLUNTEER_HEADER, [("А", "Б", "", "01.01.1990", "89160000009")]),
        django_capture_on_commit_callbacks,
    )
    other = verified_client(coordinator)
    assert other.get(reverse("imports:list")).status_code == 403
    assert other.get(job.get_absolute_url()).status_code == 403
    admin_client.post(reverse("imports:cancel", args=[job.pk]))
    job.refresh_from_db()
    assert job.status == ImportStatus.CANCELLED and not Attachment.objects.exists()
    with pytest.raises(ValidationError):
        services.confirm(job)


def test_job_page_polls_only_while_in_progress(admin_client, admin):
    job = ImportJob.objects.create(kind="volunteers", file_format="xlsx", created_by=admin)
    page = admin_client.get(job.get_absolute_url()).content.decode()
    assert 'hx-trigger="every 3s"' in page
    ImportJob.objects.filter(pk=job.pk).update(status=ImportStatus.DONE, finished_at=timezone.now())
    fragment = admin_client.get(job.get_absolute_url(), HTTP_HX_REQUEST="true")
    assert "every 3s" not in fragment.content.decode()
