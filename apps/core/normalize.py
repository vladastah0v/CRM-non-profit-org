"""Нормализация контактов и ФИО — для поиска и проверки дублей (ФТ-В-04, ФТ-П-06, п. 4.4)."""

from __future__ import annotations

import re

import phonenumbers
from django.core.exceptions import ValidationError

DEFAULT_REGION = "RU"
_SPACES = re.compile(r"\s+")
_NON_DIGITS = re.compile(r"\D+")


def normalize_phone(raw: str) -> str:
    """Телефон в формате E.164 (+79161234567). Пустая строка — пустая строка.

    Принимает «8 (916) 123-45-67», «+7 916 123 45 67», «9161234567».
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        number = phonenumbers.parse(raw, DEFAULT_REGION)
    except phonenumbers.NumberParseException as exc:
        raise ValidationError("Неверный номер телефона.", code="invalid_phone") from exc
    if not phonenumbers.is_valid_number(number):
        raise ValidationError("Неверный номер телефона.", code="invalid_phone")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def format_phone(e164: str) -> str:
    """Телефон для показа: +7 916 123-45-67."""
    if not e164:
        return ""
    try:
        number = phonenumbers.parse(e164, DEFAULT_REGION)
    except phonenumbers.NumberParseException:
        return e164
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.INTERNATIONAL)


def phone_search_digits(query: str) -> str:
    """Цифры из поискового запроса для поиска по телефону. «8 916…» → «7916…»."""
    digits = _NON_DIGITS.sub("", query or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def normalize_name(*parts: str) -> str:
    """ФИО для сравнения: нижний регистр, «ё» → «е», одиночные пробелы."""
    text = " ".join(p for p in parts if p)
    return _SPACES.sub(" ", text).strip().lower().replace("ё", "е")


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()
