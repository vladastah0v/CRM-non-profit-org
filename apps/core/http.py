from __future__ import annotations

import ipaddress

from django.conf import settings
from django.http import HttpRequest


def client_ip(request: HttpRequest) -> str | None:
    """IP-адрес клиента. За Nginx берётся из X-Real-IP — только если прокси доверенный."""
    candidates = []
    if settings.TRUST_X_REAL_IP:
        candidates.append(request.META.get("HTTP_X_REAL_IP", ""))
    candidates.append(request.META.get("REMOTE_ADDR", ""))
    for value in candidates:
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError:
            continue
    return None
