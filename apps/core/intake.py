"""Общая часть приёма анкет с сайта фонда (п. 7.1): POST /api/v1/intake/<вид>/.

- подпись HMAC-SHA256 обязательна (apps.core.signatures), запросы без неё отклоняются;
- каждая подпись принимается один раз — перехваченный запрос не повторить;
- не более 10 запросов в минуту с одного IP (защита от ботов — капча на форме сайта);
- тело — JSON не больше 64 КБ;
- ответ не сообщает, найден ли дубль: по API нельзя проверить, есть ли человек в базе.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import signatures
from .http import client_ip

logger = logging.getLogger(__name__)

MAX_BODY_SIZE = 64 * 1024

SIGNATURE_PARAMETERS = [
    OpenApiParameter("X-Timestamp", str, OpenApiParameter.HEADER, required=True),
    OpenApiParameter(
        "X-Signature",
        str,
        OpenApiParameter.HEADER,
        required=True,
        description='sha256=<hex HMAC-SHA256(секрет, "<X-Timestamp>." + тело)>',
    ),
]
INTAKE_RESPONSES = {
    201: OpenApiResponse(description="Анкета принята"),
    400: OpenApiResponse(description="Ошибки в данных анкеты"),
    403: OpenApiResponse(description="Нет подписи, подпись неверна или устарела"),
    409: OpenApiResponse(description="Этот запрос уже был принят"),
    429: OpenApiResponse(description="Слишком много запросов"),
}


class IntakeThrottle(ScopedRateThrottle):
    """Лимит по IP клиента. IP — как во всём проекте: X-Real-IP только от доверенного Nginx."""

    def get_ident(self, request: Any) -> str:
        return client_ip(request) or "unknown"


def _error(message: str, http_status: int) -> Response:
    return Response({"detail": message}, status=http_status)


class SignedIntakeView(APIView):
    """Подклассы задают serializer_class и create(serializer, request)."""

    authentication_classes: list[Any] = []  # вместо сессии — подпись HMAC
    permission_classes = [AllowAny]
    throttle_classes = [IntakeThrottle]
    throttle_scope = "intake"
    serializer_class: type[serializers.Serializer]

    def create(self, serializer: Any, request: HttpRequest) -> None:
        raise NotImplementedError

    def post(self, request: Request) -> Response:
        if not settings.INTAKE_HMAC_SECRET:
            logger.error("Приём анкет: не задан INTAKE_HMAC_SECRET")
            return _error("Приём анкет временно недоступен.", status.HTTP_503_SERVICE_UNAVAILABLE)
        if request.content_type.split(";")[0].strip() != "application/json":
            return _error("Ожидается application/json.", status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
        body = request.body
        if len(body) > MAX_BODY_SIZE:
            return _error("Слишком большой запрос.", status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        try:
            signed = signatures.verify(
                settings.INTAKE_HMAC_SECRET,
                request.headers.get("X-Timestamp"),
                request.headers.get("X-Signature"),
                body,
            )
        except signatures.SignatureError:
            return _error("Подпись запроса неверна или устарела.", status.HTTP_403_FORBIDDEN)
        if not signatures.claim(signed):
            return _error("Этот запрос уже был принят.", status.HTTP_409_CONFLICT)

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            signatures.release(signed)
            return _error("Тело запроса — не JSON.", status.HTTP_400_BAD_REQUEST)

        serializer = self.serializer_class(data=payload)
        if not serializer.is_valid():
            signatures.release(signed)
            return Response({"errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        try:
            self.create(serializer, request._request)
        except ValidationError as exc:
            signatures.release(signed)
            errors = (
                exc.message_dict
                if hasattr(exc, "error_dict")
                else {"non_field_errors": exc.messages}
            )
            return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            signatures.release(signed)
            raise
        return Response({"status": "accepted"}, status=status.HTTP_201_CREATED)
