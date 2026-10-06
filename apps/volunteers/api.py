"""Приём анкет волонтёров с сайта фонда: POST /api/v1/intake/volunteer/ (ФТ-В-03, п. 7.1).

- подпись HMAC-SHA256 обязательна (apps.core.signatures), запросы без неё отклоняются;
- не более 10 запросов в минуту с одного IP (защита от ботов — капча на форме сайта);
- отметка о согласии обязательна, факт и время согласия сохраняются;
- ответ не сообщает, найден ли дубль: по API нельзя проверить, есть ли человек в базе.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core import signatures
from apps.core.http import client_ip
from apps.core.normalize import normalize_phone

from . import services
from .models import ADULT_AGE, Weekday, telegram_username_validator

logger = logging.getLogger(__name__)

MAX_BODY_SIZE = 64 * 1024


class IntakeThrottle(ScopedRateThrottle):
    """Лимит по IP клиента. IP — как во всём проекте: X-Real-IP только от доверенного Nginx."""

    def get_ident(self, request: Any) -> str:
        return client_ip(request) or "unknown"


class AvailabilitySlotSerializer(serializers.Serializer):
    weekday = serializers.ChoiceField(choices=Weekday.choices)
    time_from = serializers.TimeField()
    time_to = serializers.TimeField()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["time_to"] <= attrs["time_from"]:
            raise serializers.ValidationError("Время «до» должно быть позже времени «с».")
        return attrs


class VolunteerIntakeSerializer(serializers.Serializer):
    last_name = serializers.CharField(max_length=150)
    first_name = serializers.CharField(max_length=150)
    middle_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    birth_date = serializers.DateField(input_formats=["%Y-%m-%d", "%d.%m.%Y"])
    phone = serializers.CharField(max_length=30)
    email = serializers.EmailField(required=False, allow_blank=True)
    telegram = serializers.CharField(max_length=33, required=False, allow_blank=True)
    city = serializers.CharField(max_length=150, required=False, allow_blank=True)
    district = serializers.CharField(max_length=200, required=False, allow_blank=True)
    skills = serializers.ListField(
        child=serializers.CharField(max_length=200), required=False, max_length=50
    )
    availability = serializers.ListField(
        child=AvailabilitySlotSerializer(), required=False, max_length=21
    )
    consent = serializers.BooleanField(
        help_text="Отметка о согласии на обработку персональных данных. Должна быть true."
    )
    guardian_name = serializers.CharField(max_length=300, required=False, allow_blank=True)
    guardian_relation = serializers.CharField(max_length=100, required=False, allow_blank=True)
    guardian_phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    guardian_email = serializers.EmailField(required=False, allow_blank=True)
    guardian_consent = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Для несовершеннолетних: согласие дал законный представитель.",
    )

    def validate_consent(self, value: bool) -> bool:
        if not value:
            raise serializers.ValidationError(
                "Без согласия на обработку данных анкету принять нельзя."
            )
        return value

    def validate_telegram(self, value: str) -> str:
        value = value.strip().lstrip("@")
        if value:
            try:
                telegram_username_validator(value)
            except ValidationError as exc:
                raise serializers.ValidationError(exc.messages) from exc
        return value

    def _phone(self, value: str) -> str:
        try:
            return normalize_phone(value)
        except ValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc

    def validate_phone(self, value: str) -> str:
        return self._phone(value)

    def validate_guardian_phone(self, value: str) -> str:
        return self._phone(value)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        birth_date = attrs["birth_date"]
        today = timezone.localdate()
        age = (
            today.year
            - birth_date.year
            - ((today.month, today.day) < (birth_date.month, birth_date.day))
        )
        if birth_date > today:
            raise serializers.ValidationError({"birth_date": "Дата рождения в будущем."})
        if age < ADULT_AGE:
            # ФТ-В-08: данные законного представителя и его согласие
            errors = {}
            if not attrs.get("guardian_name"):
                errors["guardian_name"] = "Обязательно для несовершеннолетних."
            if not attrs.get("guardian_phone"):
                errors["guardian_phone"] = "Обязательно для несовершеннолетних."
            if not attrs.get("guardian_consent"):
                errors["guardian_consent"] = "Нужно согласие законного представителя."
            if errors:
                raise serializers.ValidationError(errors)
        return attrs

    def to_application(self) -> services.IntakeApplication:
        data = self.validated_data
        fields = {
            "last_name": data["last_name"],
            "first_name": data["first_name"],
            "middle_name": data.get("middle_name", ""),
            "birth_date": data["birth_date"],
            "phone": data["phone"],
            "email": data.get("email", ""),
            "telegram_username": data.get("telegram", ""),
            "city": data.get("city", ""),
            "guardian_name": data.get("guardian_name", ""),
            "guardian_relation": data.get("guardian_relation", ""),
            "guardian_phone": data.get("guardian_phone", ""),
            "guardian_email": data.get("guardian_email", ""),
        }
        slots = [
            (int(s["weekday"]), s["time_from"], s["time_to"]) for s in data.get("availability", [])
        ]
        return services.IntakeApplication(
            data=fields,
            district_name=data.get("district", ""),
            skill_names=data.get("skills", []),
            availability=sorted(set(slots)),
        )


def _error(message: str, http_status: int) -> Response:
    return Response({"detail": message}, status=http_status)


class VolunteerIntakeView(APIView):
    authentication_classes: list[Any] = []  # вместо сессии — подпись HMAC
    permission_classes = [AllowAny]
    throttle_classes = [IntakeThrottle]
    throttle_scope = "intake"

    @extend_schema(
        summary="Анкета волонтёра с сайта фонда",
        request=VolunteerIntakeSerializer,
        parameters=[
            OpenApiParameter("X-Timestamp", str, OpenApiParameter.HEADER, required=True),
            OpenApiParameter(
                "X-Signature",
                str,
                OpenApiParameter.HEADER,
                required=True,
                description='sha256=<hex HMAC-SHA256(секрет, "<X-Timestamp>." + тело)>',
            ),
        ],
        responses={
            201: OpenApiResponse(description="Анкета принята"),
            400: OpenApiResponse(description="Ошибки в данных анкеты"),
            403: OpenApiResponse(description="Нет подписи, подпись неверна или устарела"),
            409: OpenApiResponse(description="Этот запрос уже был принят"),
            429: OpenApiResponse(description="Слишком много запросов"),
        },
    )
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

        serializer = VolunteerIntakeSerializer(data=payload)
        if not serializer.is_valid():
            signatures.release(signed)
            return Response({"errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        try:
            services.create_from_intake(serializer.to_application(), request=request._request)
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
