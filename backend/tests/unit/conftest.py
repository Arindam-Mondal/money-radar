from collections.abc import Iterator

import pytest

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker


@pytest.fixture(autouse=True)
def _db_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every unit test gets a dummy DB password unless it overrides or deletes it."""
    monkeypatch.setenv("DB_PASSWORD", "test-password")


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Settings, engine and sessionmaker are lru_cached; clear them so each test sees its own
    environment and no engine built from one test's settings leaks into the next."""
    caches = (get_settings, get_engine, get_sessionmaker)
    for cached in caches:
        cached.cache_clear()
    yield
    for cached in caches:
        cached.cache_clear()
