"""Ключевые сценарии в браузере (раздел 9.1, 9.2 ТЗ). В каждом — нет ошибок в консоли,
в том числе нарушений политики CSP (страницы идут через Nginx с рабочими заголовками)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Browser, Page, expect

from conftest import BASE_URL, PASSWORD, login, no_horizontal_scroll, shot, totp

PHONE = {"viewport": {"width": 360, "height": 740}, "is_mobile": True, "has_touch": True}


@pytest.fixture
def phone(browser: Browser) -> Page:
    context = browser.new_context(**PHONE, locale="ru-RU")
    page = context.new_page()
    yield page
    context.close()


def test_login_requires_second_factor(page: Page, errors: list[str]) -> None:
    """Вход без второго фактора невозможен (НФТ-ИБ-02)."""
    page.goto(f"{BASE_URL}/accounts/login/")
    page.get_by_label("Логин").fill("e2e-coordinator-5")
    page.get_by_label("Пароль").fill(PASSWORD)
    page.get_by_role("button", name="Продолжить").click()
    expect(page.get_by_label("Код из приложения")).to_be_visible()
    page.goto(f"{BASE_URL}/volunteers/")
    expect(page).to_have_url(re.compile(r"/accounts/login/code/"))  # дальше кода не пустит
    page.get_by_label("Код из приложения").fill("000000" if totp() != "000000" else "111111")
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.get_by_role("alert")).to_be_visible()
    # После неверного кода — пауза перед следующей попыткой (защита от подбора)
    page.wait_for_timeout(2500)
    page.get_by_label("Код из приложения").fill(totp())
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("Добро пожаловать")
    assert errors == []


def test_360px_key_pages_and_menu(phone: Page, errors: list[str]) -> None:
    """Интерфейс корректно работает на смартфоне шириной 360 px (сценарий 9.2)."""
    login(phone, "curator")
    pages = {
        "главная": "/",
        "подопечные": "/beneficiaries/",
        "заявки": "/requests/",
        "задачи": "/interactions/tasks/",
        "смены": "/shifts/?period=upcoming",
        "отчёты": "/reports/program/",
        "поиск": "/search/?q=Подопечнова",
    }
    for name, path in pages.items():
        phone.goto(f"{BASE_URL}{path}")
        assert no_horizontal_scroll(phone), f"горизонтальная прокрутка: {name}"
        shot(phone, f"360-{name}")
    phone.goto(f"{BASE_URL}/beneficiaries/")
    phone.get_by_role("link", name=re.compile("Подопечнова")).first.click()
    expect(phone.get_by_role("heading", level=1)).to_contain_text("Подопечнова")
    assert no_horizontal_scroll(phone), "горизонтальная прокрутка: карточка"
    shot(phone, "360-карточка-подопечного")
    # Мобильное меню открывается и ведёт в разделы
    phone.get_by_role("button", name="Открыть меню").click()
    menu = phone.get_by_role("navigation", name="Мобильное меню")
    expect(menu.get_by_role("link", name="Заявки")).to_be_visible()
    shot(phone, "360-меню")
    assert errors == []


def test_curator_registers_case_from_phone(phone: Page, errors: list[str]) -> None:
    """Куратор вносит данные с выезда: новый подопечный и заявка — со смартфона (п. 3.10)."""
    login(phone, "curator")
    phone.goto(f"{BASE_URL}/beneficiaries/new/")
    phone.get_by_label(re.compile(r"^Фамилия")).fill("Выездная")
    phone.get_by_label(re.compile(r"^Имя")).fill("Мария")
    phone.get_by_label("Дата рождения").fill("1950-06-01")
    phone.get_by_label("Адрес").fill("Москва, ул. Лесная, д. 7")
    phone.get_by_label("Телефон", exact=True).fill("8 903 555-66-77")
    phone.get_by_role("button", name=re.compile("Создать|Сохранить")).click()
    expect(phone.get_by_role("heading", level=1)).to_contain_text("Выездная Мария")
    assert no_horizontal_scroll(phone)
    shot(phone, "360-новый-подопечный")
    assert errors == []


def test_coordinator_filters_without_reload(page: Page, errors: list[str]) -> None:
    """Фильтры списка обновляют результаты без перезагрузки (HTMX) и меняют адрес."""
    login(page, "coordinator")
    page.goto(f"{BASE_URL}/volunteers/")
    page.get_by_label("Статус").select_option(label="Активен")
    expect(page).to_have_url(re.compile(r"status=active"))
    expect(page.locator("#volunteer-results")).to_contain_text("Волонтёрова")
    page.get_by_label("Статус").select_option(label="Анкета")
    expect(page.locator("#volunteer-results")).not_to_contain_text("Волонтёрова")
    # «Назад» возвращает прошлый фильтр, а снимки страниц с ФИО не хранятся в браузере
    page.go_back()
    expect(page).to_have_url(re.compile(r"status=active"))
    expect(page.locator("#volunteer-results")).to_contain_text("Волонтёрова")
    assert page.evaluate("localStorage.getItem('htmx-history-cache')") is None
    assert errors == []


def test_coordinator_sees_expiring_document(phone: Page, errors: list[str]) -> None:
    """Срок документа волонтёра виден в карточке (ФТ-В-09), в том числе на смартфоне."""
    login(phone, "coordinator")
    phone.goto(f"{BASE_URL}/volunteers/")
    phone.get_by_role("link", name=re.compile("Волонтёрова")).first.click()
    documents = phone.get_by_role("region", name="Документы")
    expect(documents).to_contain_text("Личная медицинская книжка")
    expect(documents).to_contain_text("истекает")
    assert no_horizontal_scroll(phone), "горизонтальная прокрутка: карточка волонтёра"
    documents.scroll_into_view_if_needed()
    # Состояние срока видно сразу, без прокрутки таблицы вбок
    expect(documents.get_by_text("истекает", exact=True)).to_be_in_viewport()
    shot(phone, "360-карточка-волонтёра")
    assert errors == []


def test_analyst_downloads_report(page: Page, errors: list[str]) -> None:
    """Отчёт по программе открывается и выгружается в Excel."""
    login(page, "analyst")
    page.goto(f"{BASE_URL}/reports/program/")
    expect(page.get_by_text("Уникальных подопечных")).to_be_visible()
    with page.expect_download() as download:
        page.get_by_role("link", name="Скачать Excel").click()
    assert download.value.suggested_filename.endswith(".xlsx")
    assert errors == []


def test_logout_ends_session(page: Page, errors: list[str]) -> None:
    login(page, "admin")
    page.get_by_role("button", name="Выйти").first.click()
    page.goto(f"{BASE_URL}/beneficiaries/")
    expect(page).to_have_url(re.compile(r"/accounts/login/"))
    assert errors == []
