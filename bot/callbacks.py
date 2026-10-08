"""Данные inline-кнопок (до 64 байт).

`a:<действие>:<id назначения без дефисов>` — кнопки назначений. Такие же кнопки
CRM присылает в напоминаниях (apps/shifts/reminders.py), поэтому формат общий.
`s:signup:<id смены>` — запись на смену из списка открытых смен.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

ASSIGNMENT_ACTIONS = frozenset({"confirm", "decline", "details", "attended", "hours", "noshow"})
SHIFT_ACTIONS = frozenset({"signup"})


@dataclass(frozen=True)
class Callback:
    kind: str  # "a" — назначение, "s" — смена
    action: str
    id: str  # UUID в каноническом виде, как в API


def assignment(action: str, pk: str) -> str:
    return f"a:{action}:{uuid.UUID(pk).hex}"


def shift(action: str, pk: str) -> str:
    return f"s:{action}:{uuid.UUID(pk).hex}"


def parse(data: str | None) -> Callback | None:
    """Разобрать данные кнопки. Чужие и испорченные данные — None."""
    parts = (data or "").split(":")
    if len(parts) != 3:
        return None
    kind, action, raw_id = parts
    allowed = {"a": ASSIGNMENT_ACTIONS, "s": SHIFT_ACTIONS}.get(kind)
    if allowed is None or action not in allowed:
        return None
    try:
        pk = uuid.UUID(hex=raw_id)
    except ValueError:
        return None
    return Callback(kind, action, str(pk))
