from collections.abc import Iterator

import pytest

from app.config import Settings, get_settings
from app.db.session import get_engine, get_sessionmaker


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests start from Settings defaults, whatever the shell or CI exported
    (CI sets DB_HOST for its migration steps). Every field's env var is removed, so new
    settings are covered automatically. Then a dummy DB password, since it's required."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
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
