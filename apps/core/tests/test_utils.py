"""Нормализация контактов, подписи HMAC, шаблонные фильтры."""

import time

import pytest
from django.core.exceptions import ValidationError
from django.test import RequestFactory

from apps.core import signatures
from apps.core.normalize import (
    format_phone,
    normalize_name,
    normalize_phone,
    phone_search_digits,
)
from apps.core.templatetags.crm import query_replace, ru_plural


@pytest.mark.parametrize(
    "raw", ["8 (916) 123-45-67", "+7 916 123 45 67", "9161234567", "+79161234567"]
)
def test_phone_normalized_to_e164(raw):
    assert normalize_phone(raw) == "+79161234567"


@pytest.mark.parametrize("raw", ["123", "abc", "+7 916 123"])
def test_invalid_phone(raw):
    with pytest.raises(ValidationError):
        normalize_phone(raw)


def test_phone_helpers():
    assert normalize_phone("") == ""
    assert format_phone("+79161234567") == "+7 916 123-45-67"
    assert phone_search_digits("8 916 123") == "8916123"
    assert phone_search_digits("8 (916) 123-45-67") == "79161234567"


def test_normalize_name():
    assert normalize_name("  Семёнова ", "АЛЁНА", "") == "семенова алена"


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (1, "1 год"),
        (2, "2 года"),
        (5, "5 лет"),
        (11, "11 лет"),
        (21, "21 год"),
        (14, "14 лет"),
        (22, "22 года"),
        (112, "112 лет"),
    ],
)
def test_ru_plural(n, expected):
    assert ru_plural(n, "год,года,лет") == expected


def test_query_replace_keeps_filters_and_resets_page():
    request = RequestFactory().get("/v/", {"status": "active", "page": "3"})
    assert query_replace(request, page=4) == "?status=active&page=4"
    assert query_replace(request, sort="-name") == "?status=active&sort=-name"
    assert query_replace(request, status="") == "?"


def test_signature_roundtrip(db):
    body = b'{"a": 1}'
    ts = str(int(time.time()))
    signed = signatures.verify("secret", ts, signatures.sign("secret", ts, body), body)
    assert signatures.claim(signed) and not signatures.claim(signed)
    signatures.release(signed)
    assert signatures.claim(signed)


@pytest.mark.parametrize(
    ("secret", "ts", "sig"),
    [("", "1", "sha256=x"), ("s", None, "sha256=x"), ("s", "abc", "sha256=x"), ("s", "1", None)],
)
def test_signature_errors(secret, ts, sig):
    with pytest.raises(signatures.SignatureError):
        signatures.verify(secret, ts, sig, b"")
