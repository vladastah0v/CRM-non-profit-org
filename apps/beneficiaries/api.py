"""Приём обращений подопечных с сайта фонда: POST /api/v1/intake/beneficiary/ (п. 7.1).

Подпись, защита от повторов и лимиты — общие для всех анкет (apps.core.intake).
Карточка создаётся со статусом «Новый» без куратора; администраторы получают
уведомление и назначают куратора. О здоровье и документах форма не спрашивает:
особые категории — только с отдельным согласием, при личной работе куратора.
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
from apps.core.normalize import normalize_phone

from . import services


class BeneficiaryIntakeSerializer(serializers.Serializer):
    last_name = serializers.CharField(max_length=150)
    first_name = serializers.CharField(max_length=150)
    middle_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    birth_date = serializers.DateField(input_formats=["%Y-%m-%d", "%d.%m.%Y"])
    phone = serializers.CharField(max_length=30)
    address = serializers.CharField(max_length=500, required=False, allow_blank=True)
    category = serializers.CharField(max_length=200, required=False, allow_blank=True)
    need = serializers.CharField(
        max_length=2000,
        required=False,
        allow_blank=True,
        help_text="Какая помощь нужна — без сведений о здоровье и документах.",
    )
    consent = serializers.BooleanField(
        help_text="Отметка о согласии на обработку персональных данных. Должна быть true."
    )
    representative_name = serializers.CharField(max_length=300, required=False, allow_blank=True)
    representative_relation = serializers.CharField(
        max_length=100, required=False, allow_blank=True
    )
    representative_phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    representative_consent = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Для несовершеннолетних: согласие дал законный представитель.",
    )

    def validate_consent(self, value: bool) -> bool:
        if not value:
            raise serializers.ValidationError(
                "Без согласия на обработку данных обращение принять нельзя."
            )
        return value

    def _phone(self, value: str) -> str:
        try:
            return normalize_phone(value)
        except ValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc

    def validate_phone(self, value: str) -> str:
        return self._phone(value)

    def validate_representative_phone(self, value: str) -> str:
        return self._phone(value)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        birth_date = attrs["birth_date"]
        if birth_date > timezone.localdate():
            raise serializers.ValidationError({"birth_date": "Дата рождения в будущем."})
        if is_minor(birth_date):
            errors = {}
            if not attrs.get("representative_name"):
                errors["representative_name"] = "Обязательно для несовершеннолетних."
            if not attrs.get("representative_phone"):
                errors["representative_phone"] = "Обязательно для несовершеннолетних."
            if not attrs.get("representative_consent"):
                errors["representative_consent"] = "Нужно согласие законного представителя."
            if errors:
                raise serializers.ValidationError(errors)
        return attrs

    def to_application(self) -> services.IntakeApplication:
        data = self.validated_data
        return services.IntakeApplication(
            data={
                "last_name": data["last_name"],
                "first_name": data["first_name"],
                "middle_name": data.get("middle_name", ""),
                "birth_date": data["birth_date"],
                "phone": data["phone"],
                "address": data.get("address", ""),
            },
            category_name=data.get("category", ""),
            need=data.get("need", ""),
            representative_name=data.get("representative_name", ""),
            representative_relation=data.get("representative_relation", ""),
            representative_phone=data.get("representative_phone", ""),
        )


class BeneficiaryIntakeView(SignedIntakeView):
    serializer_class = BeneficiaryIntakeSerializer

    def create(self, serializer: Any, request: HttpRequest) -> None:
        services.create_from_intake(serializer.to_application(), request=request)

    @extend_schema(
        summary="Обращение подопечного с сайта фонда",
        request=BeneficiaryIntakeSerializer,
        parameters=SIGNATURE_PARAMETERS,
        responses=INTAKE_RESPONSES,
    )
    def post(self, request: Request) -> Response:
        return super().post(request)
