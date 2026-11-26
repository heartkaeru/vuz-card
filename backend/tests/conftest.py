import pytest


@pytest.fixture(autouse=True)
def _reset_search_engine_cooldown():
    """Капча в одном тесте не должна «выключать» поисковик в следующих."""
    try:
        from parser import news_search
    except ImportError:  # pragma: no cover - запуск из корня репозитория
        from backend.parser import news_search
    news_search._ENGINE_BLOCKED_UNTIL.clear()
    yield
    news_search._ENGINE_BLOCKED_UNTIL.clear()
