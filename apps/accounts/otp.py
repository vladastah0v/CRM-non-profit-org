from typing import Any


def is_verified(user: Any) -> bool:
    """Подтверждён ли второй фактор. Атрибут is_verified добавляет OTPMiddleware."""
    check = getattr(user, "is_verified", None)
    return bool(user is not None and user.is_authenticated and check is not None and check())
