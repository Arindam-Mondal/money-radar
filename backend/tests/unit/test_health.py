import logging
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.main import create_app
from app.db.session import get_session


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_startup_fails_without_db_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_PASSWORD")
    with pytest.raises(ValidationError), TestClient(create_app()):
        pass


def test_startup_logs_providers_not_secrets(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="money_radar")
    with TestClient(create_app()):
        pass
    assert "classifier=laya llm=ollama" in caplog.text
    assert "test-password" not in caplog.text


def _client_with_session(session: MagicMock) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_ready_ok() -> None:
    session = MagicMock(spec=Session)
    with _client_with_session(session) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok"}}


def test_ready_db_down_is_503_without_details() -> None:
    session = MagicMock(spec=Session)
    session.execute.side_effect = OperationalError(
        "SELECT 1", {}, Exception("connection to db:5432 failed for user money_radar")
    )
    with _client_with_session(session) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": {"database": "unavailable"}}
    assert "money_radar" not in response.text


def test_liveness_ignores_db(client: TestClient) -> None:
    # /health never touches the database: it must stay 200 even if the DB is down.
    assert client.get("/health").status_code == 200
