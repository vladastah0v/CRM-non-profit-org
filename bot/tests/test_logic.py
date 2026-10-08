"""Сценарии бота с подставным API CRM: привязка, мои смены, кнопки, ввод часов."""

import asyncio
from decimal import Decimal

from bot import callbacks, logic, texts
from bot.crm import CrmClient, CrmError, CrmUnavailableError, NotLinkedError

PK = "1f0e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
SHIFT = "2a0e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


def assignment(**fields):
    return {
        "id": PK,
        "kind": "shift",
        "title": "Раздача продуктов",
        "starts_at": "2026-10-09T10:00:00+03:00",
        "ends_at": "2026-10-09T14:00:00+03:00",
        "address": "ул. Мира, 5",
        "description": "Взять перчатки",
        "status": "assigned",
        "status_display": "Назначен",
        "can_confirm": True,
        "can_decline": True,
        "needs_attendance": False,
        "default_hours": "4.00",
        "hours": None,
        **fields,
    }


class FakeCrm(CrmClient):
    """Подставной API: те же методы, что у CrmClient, без HTTP."""

    def __init__(self, *, linked=True, error=None):
        self.linked = linked
        self.error = error
        self.calls = []

    def _check(self, name, *args):
        self.calls.append((name, *args))
        if self.error:
            raise self.error
        if not self.linked and name not in ("link",):
            raise NotLinkedError("не привязан", 403)

    async def link(self, tg, code):
        self._check("link", code)
        if code != "ABCD-EFGH":
            raise CrmError("Код неверный или устарел.")
        self.linked = True
        return {"first_name": "Анна"}

    async def unlink(self, tg):
        self._check("unlink")
        return {"status": "unlinked"}

    async def me(self, tg):
        self._check("me")
        return {
            "first_name": "Анна",
            "hours_total": "12.50",
            "can_sign_up": True,
            "status_display": "Активен",
        }

    async def assignments(self, tg):
        self._check("assignments")
        return [
            assignment(),
            assignment(id=SHIFT, can_confirm=False, can_decline=False, needs_attendance=True),
        ]

    async def assignment(self, tg, pk):
        self._check("assignment", pk)
        return assignment()

    async def confirm(self, tg, pk):
        self._check("confirm", pk)
        return assignment(status="confirmed")

    async def decline(self, tg, pk):
        self._check("decline", pk)
        return assignment(status="declined")

    async def attendance(self, tg, pk, *, attended, hours=None):
        self._check("attendance", pk, attended, hours)
        return assignment(status="attended", hours=str(hours or Decimal("4.00")))

    async def open_shifts(self, tg):
        self._check("open_shifts")
        return {
            "results": [
                {
                    "id": SHIFT,
                    "title": "Сбор вещей",
                    "type": "Склад",
                    "starts_at": "2026-10-10T10:00:00+03:00",
                    "ends_at": "2026-10-10T12:00:00+03:00",
                    "address": "Склад",
                    "district": None,
                    "description": "",
                    "free_places": 2,
                }
            ]
        }

    async def signup(self, tg, pk):
        self._check("signup", pk)
        return assignment(id=pk)


def run(coro):
    return asyncio.run(coro)


def test_start_asks_code_when_not_linked_and_links_by_deep_link():
    crm = FakeCrm(linked=False)
    (reply,) = run(logic.start(crm, 1))
    assert reply.text == texts.ASK_CODE and not reply.menu
    (reply,) = run(logic.start(crm, 1, "ABCD-EFGH"))
    assert reply.text.startswith("Готово, Анна!") and reply.menu
    (reply,) = run(logic.start(crm, 1))
    assert "Всего отработано часов: 12,5" in reply.text


def test_link_with_wrong_code_shows_crm_message():
    (reply,) = run(logic.link(FakeCrm(linked=False), 1, "AAAA-BBBB"))
    assert reply.text == "Код неверный или устарел."


def test_my_shifts_buttons():
    first, second = run(logic.my_shifts(FakeCrm(), 1))
    labels = [[label for label, _ in row] for row in first.buttons]
    assert labels == [["Приду", "Не смогу"], ["Подробнее"]]
    labels = [[label for label, _ in row] for row in second.buttons]
    assert labels == [["Был(а), 4 ч"], ["Другое число часов"], ["Не смог(ла) прийти"]]
    assert "Отметьте, пожалуйста, участие." in second.text


def test_not_linked_and_unavailable_messages():
    (reply,) = run(logic.my_shifts(FakeCrm(linked=False), 1))
    assert reply.text == texts.NOT_LINKED
    (reply,) = run(logic.open_shifts(FakeCrm(error=CrmUnavailableError("x", 503)), 1))
    assert reply.text == texts.UNAVAILABLE


def test_callbacks_confirm_decline_signup_attendance():
    crm = FakeCrm()
    result = run(logic.on_callback(crm, 1, callbacks.assignment("confirm", PK)))
    assert result.remove_buttons and ("confirm", PK) in crm.calls
    result = run(logic.on_callback(crm, 1, callbacks.assignment("decline", PK)))
    assert "Координатор" in result.replies[0].text
    result = run(logic.on_callback(crm, 1, callbacks.shift("signup", SHIFT)))
    assert result.notice == "Вы записаны!" and ("signup", SHIFT) in crm.calls
    result = run(logic.on_callback(crm, 1, callbacks.assignment("attended", PK)))
    assert result.replies[0].text.startswith("Отмечено: 4 ч")
    result = run(logic.on_callback(crm, 1, callbacks.assignment("noshow", PK)))
    assert ("attendance", PK, False, None) in crm.calls
    result = run(logic.on_callback(crm, 1, callbacks.assignment("details", PK)))
    assert "Что делать: Взять перчатки" in result.replies[0].text
    assert run(logic.on_callback(crm, 1, "garbage")).notice == "Кнопка устарела."


def test_callback_rule_error_is_shown():
    crm = FakeCrm(error=CrmError("Смена уже началась — теперь отмечается явка."))
    result = run(logic.on_callback(crm, 1, callbacks.assignment("decline", PK)))
    assert result.notice.startswith("Смена уже началась") and result.remove_buttons


def test_other_hours_flow():
    crm = FakeCrm()
    result = run(logic.on_callback(crm, 1, callbacks.assignment("hours", PK)))
    assert result.ask_hours_for == PK and result.replies[0].text == texts.ASK_HOURS
    replies, done = run(logic.submit_hours(crm, 1, PK, "сто"))
    assert not done and replies[0].text == texts.BAD_HOURS
    replies, done = run(logic.submit_hours(crm, 1, PK, "2,5"))
    assert done and replies[0].text.startswith("Отмечено: 2,5 ч")
    assert ("attendance", PK, True, Decimal("2.50")) in crm.calls


def test_unlink():
    (reply,) = run(logic.unlink(FakeCrm(), 1))
    assert reply.text.startswith("Telegram отвязан")
