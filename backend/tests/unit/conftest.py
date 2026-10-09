from collections.abc import Iterator

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def _db_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every unit test gets a dummy DB password unless it overrides or deletes it."""
    monkeypatch.setenv("DB_PASSWORD", "test-password")


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """get_settings() is lru_cached; clear it so each test sees its own environment."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
