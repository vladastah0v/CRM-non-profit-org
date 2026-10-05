from typing import Any

from django.http import HttpRequest

from .roles import RoleCode, user_role_codes


def roles(request: HttpRequest) -> dict[str, Any]:
    codes = user_role_codes(getattr(request, "user", None))
    return {"user_roles": codes, "is_admin": RoleCode.ADMIN in codes}
