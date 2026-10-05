import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    """Каждый тест начинается с пустым кэшем (счётчики блокировок, лимиты запросов)."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()
