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
