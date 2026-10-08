"""Нагрузочная проверка (п. 5.3 ТЗ): до 50 одновременных сотрудников на расчётном объёме.

Цели: карточка и список — до 1 с, поиск по ФИО и телефону — до 1 с, отчёт за год —
до 10 с. Сессии (вход с 2FA уже пройден) создаёт manage.py generate_load_data.
Запуск — scripts/load/run.sh.
"""

from __future__ import annotations

import datetime
import itertools
import json
import os
import random
import re

from locust import HttpUser, between, task

with open(os.environ.get("LOAD_SESSIONS", "/mnt/out/load-sessions.json")) as f:
    SESSIONS = json.load(f)
BY_ROLE: dict[str, itertools.cycle] = {}
for role in {s["role"] for s in SESSIONS}:
    BY_ROLE[role] = itertools.cycle([s["cookie"] for s in SESSIONS if s["role"] == role])

CARD_RE = {
    "beneficiaries": re.compile(r'href="(/beneficiaries/[0-9a-f-]{36}/)"'),
    "volunteers": re.compile(r'href="(/volunteers/[0-9a-f-]{36}/)"'),
}
NAMES = ["Иванова", "Смирнов", "Кузнецова", "Петров", "Соколова", "Волков"]


class Staff(HttpUser):
    abstract = True
    role = ""
    wait_time = between(3, 8)

    def on_start(self) -> None:
        self.client.cookies.set("sessionid", next(BY_ROLE[self.role]))
        self.cards: dict[str, list[str]] = {"beneficiaries": [], "volunteers": []}

    def list_page(self, kind: str, params: str = "") -> None:
        # С параметрами в адресе список не подставляет сохранённые фильтры (без лишнего 302)
        with self.client.get(
            f"/{kind}/?sort=name{params}", name=f"список: {kind}", catch_response=True
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                return
            found = CARD_RE[kind].findall(response.text)
            if found:
                self.cards[kind] = found

    def card(self, kind: str) -> None:
        if not self.cards[kind]:
            self.list_page(kind)
        if self.cards[kind]:
            self.client.get(random.choice(self.cards[kind]), name=f"карточка: {kind}")

    def search(self) -> None:
        query = random.choice(NAMES) if random.random() < 0.5 else str(random.randrange(1000, 9999))
        self.client.get(f"/search/?q={query}", name="поиск")


class Curator(Staff):
    role = "curator"
    weight = 30

    @task(4)
    def beneficiaries(self) -> None:
        self.list_page("beneficiaries")

    @task(6)
    def beneficiary_card(self) -> None:
        self.card("beneficiaries")

    @task(3)
    def find(self) -> None:
        self.search()

    @task(2)
    def my_requests(self) -> None:
        self.client.get("/requests/?scope=mine&status=open", name="список: заявки")


class Coordinator(Staff):
    role = "coordinator"
    weight = 12

    @task(4)
    def volunteers(self) -> None:
        self.list_page("volunteers", "&status=active")

    @task(5)
    def volunteer_card(self) -> None:
        self.card("volunteers")

    @task(2)
    def shifts(self) -> None:
        self.client.get("/shifts/?period=upcoming", name="список: смены")

    @task(2)
    def find(self) -> None:
        self.search()


class Analyst(Staff):
    role = "analyst"
    weight = 4
    wait_time = between(20, 40)

    @task
    def year_report(self) -> None:
        today = datetime.date.today()
        start = today - datetime.timedelta(days=365)
        self.client.get(
            f"/reports/program/?date_from={start}&date_to={today}&program=",
            name="отчёт по программе за год",
        )

    @task
    def volunteer_report(self) -> None:
        today = datetime.date.today()
        start = today - datetime.timedelta(days=365)
        self.client.get(
            f"/reports/volunteers/?date_from={start}&date_to={today}",
            name="отчёт по волонтёрам за год",
        )


class Admin(Staff):
    role = "admin"
    weight = 4

    @task(3)
    def beneficiaries(self) -> None:
        self.list_page("beneficiaries")

    @task(3)
    def beneficiary_card(self) -> None:
        self.card("beneficiaries")

    @task(1)
    def find(self) -> None:
        self.search()
