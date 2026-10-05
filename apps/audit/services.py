from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from django.db import models
from django.http import HttpRequest

from apps.core.context import get_current_user
from apps.core.http import client_ip

from .models import AuditAction, AuditLog

logger = logging.getLogger(__name__)


def object_label(obj: models.Model) -> str:
    return f"{obj._meta.app_label}.{obj._meta.model_name}"


def log_event(
    action: AuditAction | str,
    *,
    request: HttpRequest | None = None,
    user: Any = None,
    username: str = "",
    obj: models.Model | None = None,
    object_type: str = "",
    object_id: str = "",
    fields: Iterable[str] = (),
    extra: dict[str, Any] | None = None,
) -> AuditLog:
    """Записать событие в журнал аудита.

    В `extra` нельзя передавать персональные данные — только служебные сведения
    (например, фильтры выгрузки или число записей).
    """
    if user is None and request is not None and request.user.is_authenticated:
        user = request.user
    if user is None:
        user = get_current_user()
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    if obj is not None:
        object_type = object_label(obj)
        object_id = str(obj.pk)
    return AuditLog.objects.create(
        action=action,
        user=user,
        username=username or (user.get_username() if user is not None else ""),
        object_type=object_type,
        object_id=object_id,
        fields=sorted(fields),
        ip=client_ip(request) if request is not None else None,
        user_agent=(request.headers.get("User-Agent", "")[:255] if request is not None else ""),
        extra=extra or {},
    )
