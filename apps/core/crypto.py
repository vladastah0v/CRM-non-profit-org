"""Шифрование чувствительных данных на уровне приложения (НФТ-ИБ-05, ФТ-Д-05).

Алгоритм — AES-256-GCM. Ключи хранятся вне базы, в переменной окружения
FIELD_ENCRYPTION_KEYS: «id1:base64ключ,id2:base64ключ». Первый ключ — текущий, им шифруются
новые данные; остальные нужны только для расшифровки старых (ротация ключей).

Формат шифротекста: b"v1:" + id ключа + b":" + nonce(12 байт) + шифротекст с тегом.
Для полей БД он дополнительно кодируется в base64.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

VERSION = b"v1"
NONCE_SIZE = 12
KEY_SIZE = 32


class DecryptionError(Exception):
    """Данные повреждены, подменены или зашифрованы неизвестным ключом."""


@dataclass(frozen=True)
class Keyring:
    primary_id: str
    keys: dict[str, bytes]


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(KEY_SIZE)).decode()


def parse_keys(raw: str) -> Keyring:
    keys: dict[str, bytes] = {}
    primary_id = ""
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        key_id, sep, encoded = item.partition(":")
        if not sep or not key_id.isalnum():
            raise ImproperlyConfigured("FIELD_ENCRYPTION_KEYS: ожидается формат id:ключ,id:ключ")
        try:
            key = base64.urlsafe_b64decode(encoded)
        except (binascii.Error, ValueError) as exc:
            raise ImproperlyConfigured(f"FIELD_ENCRYPTION_KEYS: ключ {key_id} не base64") from exc
        if len(key) != KEY_SIZE:
            raise ImproperlyConfigured(f"FIELD_ENCRYPTION_KEYS: ключ {key_id} должен быть 32 байта")
        if key_id in keys:
            raise ImproperlyConfigured(f"FIELD_ENCRYPTION_KEYS: ключ {key_id} указан дважды")
        keys[key_id] = key
        primary_id = primary_id or key_id
    if not keys:
        raise ImproperlyConfigured("Не задана переменная окружения FIELD_ENCRYPTION_KEYS")
    return Keyring(primary_id=primary_id, keys=keys)


@lru_cache(maxsize=1)
def _keyring_for(raw: str) -> Keyring:
    return parse_keys(raw)


def get_keyring() -> Keyring:
    return _keyring_for(settings.FIELD_ENCRYPTION_KEYS)


def encrypt_bytes(plaintext: bytes, *, aad: bytes = b"") -> bytes:
    ring = get_keyring()
    nonce = os.urandom(NONCE_SIZE)
    key_id = ring.primary_id.encode()
    header = VERSION + b":" + key_id + b":"
    ciphertext = AESGCM(ring.keys[ring.primary_id]).encrypt(nonce, plaintext, header + aad)
    return header + nonce + ciphertext


def _split(data: bytes) -> tuple[str, bytes, bytes]:
    try:
        version, key_id, rest = data.split(b":", 2)
    except ValueError as exc:
        raise DecryptionError("Неизвестный формат шифротекста") from exc
    if version != VERSION or len(rest) <= NONCE_SIZE:
        raise DecryptionError("Неизвестный формат шифротекста")
    try:
        key_id_str = key_id.decode("ascii")
    except UnicodeDecodeError as exc:
        raise DecryptionError("Неизвестный формат шифротекста") from exc
    return key_id_str, rest[:NONCE_SIZE], rest[NONCE_SIZE:]


def key_id_of(data: bytes) -> str:
    return _split(data)[0]


def decrypt_bytes(data: bytes, *, aad: bytes = b"") -> bytes:
    key_id, nonce, ciphertext = _split(data)
    key = get_keyring().keys.get(key_id)
    if key is None:
        raise DecryptionError(f"Нет ключа «{key_id}» для расшифровки")
    header = VERSION + b":" + key_id.encode() + b":"
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, header + aad)
    except InvalidTag as exc:
        raise DecryptionError("Данные повреждены или подменены") from exc


def encrypt_str(value: str, *, aad: bytes = b"") -> str:
    return base64.b64encode(encrypt_bytes(value.encode(), aad=aad)).decode()


def decrypt_str(value: str, *, aad: bytes = b"") -> str:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DecryptionError("Неизвестный формат шифротекста") from exc
    return decrypt_bytes(raw, aad=aad).decode()


def needs_rotation(value: str) -> bool:
    """True, если значение (base64 из БД) зашифровано не текущим ключом."""
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DecryptionError("Неизвестный формат шифротекста") from exc
    return key_id_of(raw) != get_keyring().primary_id
