"""Импорт строк: волонтёры, подопечные, история помощи (ФТ-А-05, п. 7.3).

Каждая строка идёт через те же сервисы, что ручной ввод, — с теми же правилами
(статусы, согласия, дубли), в отдельной точке сохранения: ошибка в строке не мешает
остальным. Предпросмотр — тот же импорт в транзакции, которая откатывается.

Совпадения:
- волонтёр — тот же телефон и дата рождения → карточка обновляется; похожий по ФИО
  или email — создаётся новая с пометкой «возможный дубль»;
- подопечный — те же ФИО и дата рождения → обновляется; похожий — новая с пометкой;
- факт помощи — тот же подопечный, тип, дата, количество и сумма → пропускается
  (повторная загрузка того же файла не задвоит историю).
"""

from __future__ import annotations

import datetime
import re
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Model

from apps.accounts.models import User
from apps.accounts.roles import RoleCode, user_has_role
from apps.beneficiaries import services as beneficiary_services
from apps.beneficiaries.models import Beneficiary, CaseStatus
from apps.consents.models import Consent, ConsentChannel, ConsentKind, ConsentSigner
from apps.consents.services import create_consent
from apps.core.dates import is_minor
from apps.core.models import (
    AssistanceType,
    BeneficiaryCategory,
    BeneficiarySource,
    District,
    LeavingReason,
    Program,
    Skill,
    TrainingType,
    VolunteerSource,
)
from apps.core.normalize import normalize_name, normalize_phone
from apps.requests import services as request_services
from apps.requests.models import Assistance
from apps.volunteers import services as volunteer_services
from apps.volunteers.models import Volunteer, VolunteerStatus

from .models import ImportKind
from .reader import Table, normalize_header

MAX_REPORTED_ERRORS = 500
MAX_REPORTED_DUPLICATES = 200
IMPORT_COMMENT = "Импорт"


@dataclass(frozen=True)
class Column:
    header: str
    key: str
    required: bool = False
    help: str = ""


class RowError(Exception):
    def __init__(self, column: str, message: str) -> None:
        super().__init__(message)
        self.column = column
        self.message = message


@dataclass
class Outcome:
    action: str  # create | update | skipped
    duplicate: str = ""
    url: str = ""


# ---------------------------------------------------------------------------
# Разбор значений
# ---------------------------------------------------------------------------
def parse_date(value: Any, column: str) -> datetime.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise RowError(column, "Дата — в формате ДД.ММ.ГГГГ.")


def parse_decimal(value: Any, column: str) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal | int):
        return Decimal(value)
    text = re.sub(r"[\s ₽]", "", str(value)).replace(",", ".")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise RowError(column, "Ожидается число.") from exc
    if not number.is_finite():
        raise RowError(column, "Ожидается число.")
    return number


def split_list(value: Any) -> list[str]:
    return [part.strip() for part in re.split(r"[;,\n]", str(value or "")) if part.strip()]


class References:
    """Справочники по названию без учёта регистра и лишних пробелов."""

    def __init__(self) -> None:
        self._cache: dict[type[Model], dict[str, Any]] = {}

    def _index(self, model: type[Model]) -> dict[str, Any]:
        if model not in self._cache:
            index: dict[str, Any] = {}
            for item in model.objects.filter(is_archived=False):  # type: ignore[attr-defined]
                index.setdefault(normalize_name(item.name), item)
            self._cache[model] = index
        return self._cache[model]

    def one(self, model: type[Model], value: Any, column: str) -> Any:
        if value in (None, ""):
            return None
        item = self._index(model).get(normalize_name(str(value)))
        if item is None:
            raise RowError(column, f"Нет в справочнике «{model._meta.verbose_name_plural}».")
        return item

    def many(self, model: type[Model], value: Any, column: str) -> list[Any]:
        return [self.one(model, name, column) for name in split_list(value)]


def status_path(
    current: str, target: str, transitions: Mapping[str, Iterable[str]], *, avoid: set[str]
) -> list[str]:
    """Кратчайшая цепочка переходов (по правилам п. 4.3) от текущего статуса к нужному."""
    if current == target:
        return []
    queue: deque[tuple[str, list[str]]] = deque([(current, [])])
    seen = {current}
    while queue:
        status, path = queue.popleft()
        for nxt in transitions[status]:
            if nxt in seen or (nxt in avoid and nxt != target):
                continue
            if nxt == target:
                return [*path, nxt]
            seen.add(nxt)
            queue.append((nxt, [*path, nxt]))
    raise RowError("Статус", "Такой переход статуса не предусмотрен.")


# ---------------------------------------------------------------------------
# Базовый импортёр
# ---------------------------------------------------------------------------
class Importer:
    kind: str
    title: str
    columns: tuple[Column, ...]

    def __init__(self, user: User) -> None:
        self.user = user
        self.refs = References()
        self.created: set[Any] = set()
        self._by_key = {c.key: c for c in self.columns}

    # --- значения строки по ключу колонки ---
    def header_of(self, key: str) -> str:
        return self._by_key[key].header

    def text(self, row: dict[str, Any], key: str) -> str:
        value = row.get(normalize_header(self.header_of(key)), "")
        if isinstance(value, Decimal):
            value = format(value, "f")
        text = " ".join(str(value).split())
        if self._by_key[key].required and not text:
            raise RowError(self.header_of(key), "Обязательная колонка не заполнена.")
        return text

    def value(self, row: dict[str, Any], key: str) -> Any:
        value = row.get(normalize_header(self.header_of(key)))
        if self._by_key[key].required and value in (None, ""):
            raise RowError(self.header_of(key), "Обязательная колонка не заполнена.")
        return value

    def date(self, row: dict[str, Any], key: str) -> datetime.date | None:
        return parse_date(self.value(row, key), self.header_of(key))

    def required_date(self, row: dict[str, Any], key: str) -> datetime.date:
        value = self.date(row, key)
        if value is None:
            raise RowError(self.header_of(key), "Обязательная колонка не заполнена.")
        return value

    def phone(self, row: dict[str, Any], key: str) -> str:
        raw = self.text(row, key)
        if not raw:
            return ""
        try:
            return normalize_phone(raw)
        except ValidationError as exc:
            raise RowError(self.header_of(key), " ".join(exc.messages)) from exc

    def missing_columns(self, table: Table) -> list[str]:
        present = set(table.header)
        return [
            c.header
            for c in self.columns
            if c.required and normalize_header(c.header) not in present
        ]

    def column_for(self, field: str) -> str:
        """Ошибка сервиса по полю модели → название колонки файла."""
        column = self._by_key.get(field)
        return column.header if column else ""

    def process_row(self, row: dict[str, Any]) -> Outcome:
        raise NotImplementedError


def _status_value(text: str, choices: Iterable[tuple[str, str]], column: str) -> str | None:
    if not text:
        return None
    wanted = normalize_name(text)
    for value, label in choices:
        if normalize_name(label) == wanted:
            return value
    names = ", ".join(label for _, label in choices)
    raise RowError(column, f"Неизвестный статус. Допустимо: {names}.")


def _nonempty(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value not in ("", None)}


def _duplicate_url(matches: list[Any], created: set[Any]) -> str:
    for match in matches:
        if match.person.pk not in created:
            return match.person.get_absolute_url()
    return ""


# ---------------------------------------------------------------------------
# Волонтёры
# ---------------------------------------------------------------------------
class VolunteerImporter(Importer):
    kind = ImportKind.VOLUNTEERS
    title = "Волонтёры"
    columns = (
        Column("Фамилия", "last_name", required=True),
        Column("Имя", "first_name", required=True),
        Column("Отчество", "middle_name"),
        Column("Дата рождения", "birth_date", required=True, help="ДД.ММ.ГГГГ"),
        Column("Телефон", "phone", required=True, help="Любой формат, например 8 916 123-45-67"),
        Column("Email", "email"),
        Column("Telegram", "telegram_username", help="Имя пользователя, можно с @"),
        Column("Город", "city"),
        Column("Район", "district", help="Как в справочнике «Районы»"),
        Column("Навыки", "skills", help="Через точку с запятой, как в справочнике"),
        Column("Программы", "programs", help="Через точку с запятой, как в справочнике"),
        Column("Источник прихода", "source", help="Как в справочнике"),
        Column("Законный представитель", "guardian_name", help="Для несовершеннолетних"),
        Column("Телефон представителя", "guardian_phone"),
        Column("Дата согласия", "consent_date", help="Бумажное согласие на обработку ПДн"),
        Column("Обучение", "training", help="Как в справочнике «Типы обучения»"),
        Column("Дата обучения", "training_date"),
        Column(
            "Статус",
            "status",
            help="Анкета (по умолчанию), Собеседование, Обучение, Активен, Пауза, Выбыл. "
            "Для «Активен» нужны дата согласия и обучение",
        ),
        Column("Причина выбытия", "leaving_reason", help="Для статуса «Выбыл»"),
    )

    def process_row(self, row: dict[str, Any]) -> Outcome:
        phone = self.phone(row, "phone")
        birth_date = self.required_date(row, "birth_date")
        data = {
            "last_name": self.text(row, "last_name"),
            "first_name": self.text(row, "first_name"),
            "middle_name": self.text(row, "middle_name"),
            "birth_date": birth_date,
            "phone": phone,
            "email": self.text(row, "email"),
            "telegram_username": self.text(row, "telegram_username").lstrip("@"),
            "city": self.text(row, "city"),
            "district": self.refs.one(District, self.value(row, "district"), "Район"),
            "source": self.refs.one(VolunteerSource, self.value(row, "source"), "Источник прихода"),
            "guardian_name": self.text(row, "guardian_name"),
            "guardian_phone": self.phone(row, "guardian_phone"),
        }
        skills = self.refs.many(Skill, self.value(row, "skills"), "Навыки")
        programs = self.refs.many(Program, self.value(row, "programs"), "Программы")
        target = _status_value(self.text(row, "status"), VolunteerStatus.choices, "Статус")
        reason = self.refs.one(LeavingReason, self.value(row, "leaving_reason"), "Причина выбытия")
        consent_date = self.date(row, "consent_date")
        training = self.refs.one(TrainingType, self.value(row, "training"), "Обучение")
        training_date = self.date(row, "training_date")
        if training is not None and training_date is None:
            raise RowError("Дата обучения", "Укажите дату обучения.")

        existing = Volunteer.objects.filter(
            phone=phone, birth_date=birth_date, is_archived=False
        ).first()
        outcome = Outcome("update" if existing else "create")
        if existing is not None:
            volunteer = volunteer_services.update_volunteer(
                existing,
                _nonempty(data),
                skills={*existing.skills.all(), *skills} if skills else None,
                programs={*existing.programs.all(), *programs} if programs else None,
            )
        else:
            matches = volunteer_services.find_duplicates(
                phone=phone,
                email=data["email"],
                last_name=data["last_name"],
                first_name=data["first_name"],
                middle_name=data["middle_name"],
                birth_date=birth_date,
            )
            volunteer = volunteer_services.create_volunteer(
                data, skills=skills, programs=programs, confirm_duplicates=True
            )
            self.created.add(volunteer.pk)
            if matches:
                volunteer.is_possible_duplicate = True
                volunteer.save(update_fields=["is_possible_duplicate"])
                outcome.duplicate = "Похож на существующую карточку волонтёра — помечен как дубль."
                outcome.url = _duplicate_url(matches, self.created)

        if (
            consent_date
            and not Consent.objects.filter(
                volunteer=volunteer, signed_on=consent_date, is_archived=False
            ).exists()
        ):
            minor = is_minor(volunteer.birth_date)
            create_consent(
                volunteer=volunteer,
                kind=ConsentKind.GENERAL,
                signed_on=consent_date,
                signer=ConsentSigner.REPRESENTATIVE if minor else ConsentSigner.SUBJECT,
                signer_name=volunteer.guardian_name if minor else "",
                channel=ConsentChannel.PAPER,
            )
        if (
            training is not None
            and training_date is not None
            and not volunteer.trainings.filter(
                training_type=training, completed_on=training_date
            ).exists()
        ):
            volunteer_services.add_training(
                volunteer, training_type=training, completed_on=training_date
            )
        if target is not None:
            path = status_path(
                volunteer.status,
                target,
                volunteer_services.TRANSITIONS,
                avoid={VolunteerStatus.LEFT},
            )
            for step in path:
                if step == VolunteerStatus.LEFT and reason is None:
                    raise RowError("Причина выбытия", "Для статуса «Выбыл» нужна причина.")
                volunteer_services.change_status(
                    volunteer, step, reason=reason, comment=IMPORT_COMMENT
                )
        return outcome


# ---------------------------------------------------------------------------
# Подопечные
# ---------------------------------------------------------------------------
class BeneficiaryImporter(Importer):
    kind = ImportKind.BENEFICIARIES
    title = "Подопечные"
    columns = (
        Column("Фамилия", "last_name", required=True),
        Column("Имя", "first_name", required=True),
        Column("Отчество", "middle_name"),
        Column("Дата рождения", "birth_date", required=True, help="ДД.ММ.ГГГГ"),
        Column("Адрес", "address"),
        Column("Телефон", "phone"),
        Column("Категория", "category", help="Как в справочнике «Категории подопечных»"),
        Column("Источник обращения", "source", help="Как в справочнике"),
        Column("Куратор", "curator", help="Логин сотрудника с ролью «Куратор»"),
        Column("Программы", "programs", help="Через точку с запятой"),
        Column("Дата согласия", "consent_date", help="Бумажное согласие на обработку ПДн"),
        Column(
            "Статус",
            "status",
            help="Новый (по умолчанию), Оценка, В работе, На паузе. "
            "Для «В работе» нужны дата согласия и куратор",
        ),
    )
    statuses = [(v, label) for v, label in CaseStatus.choices if v != CaseStatus.CLOSED]

    def _curator(self, row: dict[str, Any]) -> User | None:
        login = self.text(row, "curator")
        if not login:
            return None
        user = User.objects.filter(username__iexact=login, is_active=True).first()
        if user is None or not user_has_role(user, RoleCode.CURATOR):
            raise RowError("Куратор", "Нет активного сотрудника-куратора с таким логином.")
        return user

    def process_row(self, row: dict[str, Any]) -> Outcome:
        birth_date = self.required_date(row, "birth_date")
        data = {
            "last_name": self.text(row, "last_name"),
            "first_name": self.text(row, "first_name"),
            "middle_name": self.text(row, "middle_name"),
            "birth_date": birth_date,
            "address": self.text(row, "address"),
            "phone": self.phone(row, "phone"),
            "category": self.refs.one(
                BeneficiaryCategory, self.value(row, "category"), "Категория"
            ),
            "source": self.refs.one(
                BeneficiarySource, self.value(row, "source"), "Источник обращения"
            ),
        }
        curator = self._curator(row)
        programs = self.refs.many(Program, self.value(row, "programs"), "Программы")
        target = _status_value(self.text(row, "status"), self.statuses, "Статус")
        consent_date = self.date(row, "consent_date")

        name = normalize_name(data["last_name"], data["first_name"], data["middle_name"])
        existing = Beneficiary.objects.filter(
            search_name=name, birth_date=birth_date, is_archived=False
        ).first()
        outcome = Outcome("update" if existing else "create")
        if existing is not None:
            beneficiary = beneficiary_services.update_beneficiary(
                existing,
                _nonempty(data),
                programs={*existing.programs.all(), *programs} if programs else None,
            )
            if curator is not None and curator.pk != beneficiary.curator_id:
                beneficiary_services.assign_curator(beneficiary, curator)
        else:
            matches = beneficiary_services.find_duplicates(
                last_name=data["last_name"],
                first_name=data["first_name"],
                middle_name=data["middle_name"],
                birth_date=birth_date,
                phone=data["phone"],
            )
            beneficiary = beneficiary_services.create_beneficiary(
                data, curator=curator, programs=programs, confirm_duplicates=True
            )
            self.created.add(beneficiary.pk)
            if matches:
                beneficiary.is_possible_duplicate = True
                beneficiary.save(update_fields=["is_possible_duplicate"])
                outcome.duplicate = (
                    "Похож на существующую карточку подопечного — помечен как дубль."
                )
                outcome.url = _duplicate_url(matches, self.created)

        if (
            consent_date
            and not Consent.objects.filter(
                beneficiary=beneficiary, signed_on=consent_date, is_archived=False
            ).exists()
        ):
            minor = is_minor(beneficiary.birth_date)
            if minor:
                raise RowError(
                    "Дата согласия",
                    "За несовершеннолетнего согласие подписывает представитель — "
                    "внесите его в карточке вручную.",
                )
            create_consent(
                beneficiary=beneficiary,
                kind=ConsentKind.GENERAL,
                signed_on=consent_date,
                channel=ConsentChannel.PAPER,
            )
        if target is not None:
            path = status_path(
                beneficiary.status,
                target,
                beneficiary_services.TRANSITIONS,
                avoid={CaseStatus.CLOSED},
            )
            for step in path:
                beneficiary_services.change_status(beneficiary, step, comment=IMPORT_COMMENT)
        return outcome


# ---------------------------------------------------------------------------
# История помощи
# ---------------------------------------------------------------------------
class AssistanceImporter(Importer):
    kind = ImportKind.ASSISTANCE
    title = "История помощи"
    columns = (
        Column("Фамилия подопечного", "last_name", required=True),
        Column("Имя подопечного", "first_name", required=True),
        Column("Отчество подопечного", "middle_name"),
        Column("Дата рождения подопечного", "birth_date", required=True, help="ДД.ММ.ГГГГ"),
        Column("Тип помощи", "assistance_type", required=True, help="Как в справочнике"),
        Column("Дата", "provided_on", required=True, help="Дата оказания помощи"),
        Column("Количество", "quantity", help="Количество или сумма — хотя бы одно"),
        Column("Единица", "unit", help="По умолчанию — из справочника типов помощи"),
        Column("Сумма", "amount", help="В рублях"),
        Column("Программа", "program", help="Как в справочнике"),
        Column(
            "Исполнитель",
            "executor",
            help="Логин сотрудника или телефон волонтёра. Пусто — тот, кто загружает файл",
        ),
        Column("Комментарий", "comment"),
    )

    def _beneficiary(self, row: dict[str, Any]) -> Beneficiary:
        name = normalize_name(
            self.text(row, "last_name"), self.text(row, "first_name"), self.text(row, "middle_name")
        )
        found = list(
            Beneficiary.objects.filter(
                search_name=name,
                birth_date=self.required_date(row, "birth_date"),
                is_archived=False,
            )[:2]
        )
        if not found:
            raise RowError(
                "Фамилия подопечного", "Подопечный с такими ФИО и датой рождения не найден."
            )
        if len(found) > 1:
            raise RowError(
                "Фамилия подопечного", "Найдено несколько подопечных — уточните ФИО в базе."
            )
        return found[0]

    def _executor(self, row: dict[str, Any]) -> dict[str, Any]:
        value = self.text(row, "executor")
        if not value:
            return {"executor_user": self.user, "executor_volunteer": None}
        if len(re.sub(r"\D", "", value)) >= 10:
            try:
                phone = normalize_phone(value)
            except ValidationError as exc:
                raise RowError("Исполнитель", " ".join(exc.messages)) from exc
            volunteers = list(Volunteer.objects.filter(phone=phone, is_archived=False)[:2])
            if len(volunteers) != 1:
                raise RowError(
                    "Исполнитель", "Волонтёр с таким телефоном не найден или их несколько."
                )
            return {"executor_user": None, "executor_volunteer": volunteers[0]}
        user = User.objects.filter(username__iexact=value, is_active=True).first()
        if user is None:
            raise RowError("Исполнитель", "Нет активного сотрудника с таким логином.")
        return {"executor_user": user, "executor_volunteer": None}

    def process_row(self, row: dict[str, Any]) -> Outcome:
        beneficiary = self._beneficiary(row)
        kind = self.refs.one(AssistanceType, self.value(row, "assistance_type"), "Тип помощи")
        provided_on = self.required_date(row, "provided_on")
        quantity = parse_decimal(self.value(row, "quantity"), "Количество")
        amount = parse_decimal(self.value(row, "amount"), "Сумма")
        data = {
            "assistance_type": kind,
            "provided_on": provided_on,
            "quantity": quantity,
            "unit": self.text(row, "unit"),
            "amount": amount,
            "program": self.refs.one(Program, self.value(row, "program"), "Программа"),
            "comment": self.text(row, "comment"),
            **self._executor(row),
        }
        if Assistance.objects.filter(
            beneficiary=beneficiary,
            assistance_type=kind,
            provided_on=provided_on,
            quantity=quantity,
            amount=amount,
            is_archived=False,
        ).exists():
            return Outcome("skipped", duplicate="Такой факт помощи уже есть — строка пропущена.")
        request_services.import_assistance(beneficiary, data)
        return Outcome("create")


IMPORTERS: dict[str, type[Importer]] = {
    ImportKind.VOLUNTEERS: VolunteerImporter,
    ImportKind.BENEFICIARIES: BeneficiaryImporter,
    ImportKind.ASSISTANCE: AssistanceImporter,
}


# ---------------------------------------------------------------------------
# Прогон файла
# ---------------------------------------------------------------------------
@dataclass
class RunResult:
    summary: dict[str, int]
    errors: list[dict[str, Any]]
    duplicates: list[dict[str, Any]]


def _errors_of(importer: Importer, exc: Exception) -> list[tuple[str, str]]:
    if isinstance(exc, RowError):
        return [(exc.column, exc.message)]
    if not isinstance(exc, ValidationError):  # pragma: no cover — ловятся только эти два
        return [("", str(exc))]
    if hasattr(exc, "error_dict"):
        return [
            (importer.column_for(field), message)
            for field, messages in exc.message_dict.items()
            for message in messages
        ]
    return [("", message) for message in exc.messages]


def run(importer: Importer, table: Table) -> RunResult:
    summary = {
        "rows": len(table.rows),
        "create": 0,
        "update": 0,
        "skipped": 0,
        "duplicates": 0,
        "errors": 0,
    }
    errors: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    missing = importer.missing_columns(table)
    if missing:
        summary["errors"] = len(table.rows)
        errors.append(
            {"row": 1, "column": "", "message": "Нет обязательных колонок: " + ", ".join(missing)}
        )
        return RunResult(summary, errors, duplicates)
    for number, row in table.rows:
        try:
            with transaction.atomic():
                outcome = importer.process_row(row)
        except (RowError, ValidationError) as exc:
            summary["errors"] += 1
            for column, message in _errors_of(importer, exc):
                if len(errors) < MAX_REPORTED_ERRORS:
                    errors.append({"row": number, "column": column, "message": message})
            continue
        summary[outcome.action] += 1
        if outcome.duplicate:
            summary["duplicates"] += 1
            if len(duplicates) < MAX_REPORTED_DUPLICATES:
                duplicates.append({"row": number, "message": outcome.duplicate, "url": outcome.url})
    return RunResult(summary, errors, duplicates)


def preview(importer: Importer, table: Table) -> RunResult:
    """Предпросмотр: тот же импорт, но транзакция откатывается — в базе ничего не меняется."""
    with transaction.atomic():
        result = run(importer, table)
        transaction.set_rollback(True)
    return result
