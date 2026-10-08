import datetime

from django.utils import timezone

ADULT_AGE = 18


def full_years(birth_date: datetime.date, day: datetime.date | None = None) -> int:
    """Полных лет на дату (по умолчанию — сегодня по часовому поясу фонда)."""
    day = day or timezone.localdate()
    before_birthday = (day.month, day.day) < (birth_date.month, birth_date.day)
    return day.year - birth_date.year - before_birthday


def is_minor(birth_date: datetime.date | None) -> bool:
    return birth_date is not None and full_years(birth_date) < ADULT_AGE


# ч. 5 ст. 21 152-ФЗ: после отзыва согласия данные уничтожаются в срок до 30 дней
ERASURE_DEADLINE = datetime.timedelta(days=30)


def erasure_deadline(requested_on: datetime.date | None) -> datetime.date | None:
    return None if requested_on is None else requested_on + ERASURE_DEADLINE


# ФТ-У-04: о сроке согласия или документа напоминаем за 30 дней
EXPIRY_NOTICE_DAYS = 30


def expires_soon(valid_until: datetime.date | None, day: datetime.date | None = None) -> bool:
    """Срок истекает в ближайшие EXPIRY_NOTICE_DAYS дней (но ещё не истёк)."""
    day = day or timezone.localdate()
    return valid_until is not None and day <= valid_until <= day + datetime.timedelta(
        days=EXPIRY_NOTICE_DAYS
    )
