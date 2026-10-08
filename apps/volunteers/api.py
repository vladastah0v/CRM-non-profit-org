"""Приём анкет волонтёров с сайта фонда: POST /api/v1/intake/volunteer/ (ФТ-В-03, п. 7.1).

Подпись, защита от повторов и лимиты — общие для всех анкет (apps.core.intake).
Отметка о согласии обязательна, факт и время согласия сохраняются.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.http import HttpRequest
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.dates import is_minor
from apps.core.intake import INTAKE_RESPONSES, SIGNATURE_PARAMETERS, SignedIntakeView
from apps.core.intake import IntakeThrottle as IntakeThrottle  # прежнее место импорта
from apps.core.normalize import normalize_phone

from . import services
from .models import Weekday, telegram_username_validator


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
        if birth_date > timezone.localdate():
            raise serializers.ValidationError({"birth_date": "Дата рождения в будущем."})
        if is_minor(birth_date):
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


class VolunteerIntakeView(SignedIntakeView):
    serializer_class = VolunteerIntakeSerializer

    def create(self, serializer: Any, request: HttpRequest) -> None:
        services.create_from_intake(serializer.to_application(), request=request)

    @extend_schema(
        summary="Анкета волонтёра с сайта фонда",
        request=VolunteerIntakeSerializer,
        parameters=SIGNATURE_PARAMETERS,
        responses=INTAKE_RESPONSES,
    )
    def post(self, request: Request) -> Response:
        return super().post(request)
