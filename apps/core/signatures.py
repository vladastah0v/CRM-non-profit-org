"""Подпись запросов с сайта фонда: HMAC-SHA256 (п. 7.1 ТЗ).

Сайт отправляет заголовки:
    X-Timestamp: <unix-время в секундах>
    X-Signature: sha256=<hex HMAC-SHA256(секрет, "<X-Timestamp>." + тело запроса)>

Время входит в подпись и проверяется окном ±5 минут, а каждая подпись принимается
один раз: перехваченный запрос нельзя отправить повторно.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

from django.core.cache import cache

SIGNATURE_PREFIX = "sha256="
MAX_CLOCK_SKEW = 5 * 60


class SignatureError(Exception):
    """Подпись отсутствует, неверна или устарела."""


@dataclass(frozen=True)
class SignedRequest:
    timestamp: int
    signature: str

    @property
    def replay_key(self) -> str:
        return "signed-request:" + hashlib.sha256(self.signature.encode()).hexdigest()


def sign(secret: str, timestamp: int | str, body: bytes) -> str:
    """Подпись в формате заголовка X-Signature (для тестов и примеров интеграции)."""
    message = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


def verify(secret: str, timestamp: str | None, signature: str | None, body: bytes) -> SignedRequest:
    """Проверить подпись и срок. Повтор проверяется отдельно: claim() / release()."""
    if not secret:
        raise SignatureError("Секрет подписи не настроен")
    if not timestamp or not signature or not signature.startswith(SIGNATURE_PREFIX):
        raise SignatureError("Нет подписи")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise SignatureError("Неверное время") from exc
    if abs(time.time() - ts) > MAX_CLOCK_SKEW:
        raise SignatureError("Подпись устарела")
    # Сравнение за постоянное время — без утечки подписи по времени ответа
    if not hmac.compare_digest(sign(secret, timestamp, body), signature.strip()):
        raise SignatureError("Неверная подпись")
    return SignedRequest(ts, signature.strip())


def claim(signed: SignedRequest) -> bool:
    """Отметить подпись использованной. False — такой запрос уже принимался."""
    return bool(cache.add(signed.replay_key, 1, timeout=2 * MAX_CLOCK_SKEW))


def release(signed: SignedRequest) -> None:
    """Запрос не обработан (ошибка в данных) — сайт может отправить его повторно."""
    cache.delete(signed.replay_key)
