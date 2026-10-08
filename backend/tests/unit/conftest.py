import pytest


@pytest.fixture(autouse=True)
def _db_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every unit test gets a dummy DB password unless it overrides or deletes it."""
    monkeypatch.setenv("DB_PASSWORD", "test-password")
