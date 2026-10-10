"""Fixtures for tests against a real, migrated Postgres.

Each test runs inside one outer transaction that is rolled back afterwards, so tests
never see each other's rows and never leave data behind, even in a dev database.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import Settings


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    settings = Settings()  # DB_HOST/DB_PORT/DB_PASSWORD... from the environment
    engine = create_engine(settings.database_url, connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as e:
        pytest.fail(
            f"Postgres not reachable at {settings.db_host}:{settings.db_port}. "
            "Locally: stack up, then run with DB_HOST=127.0.0.1 DB_PORT=5433 "
            f"uv run --env-file ../.env pytest -m integration ({e.__class__.__name__})",
            pytrace=False,
        )
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with engine.connect() as connection:
        outer = connection.begin()
        # Code under test may call session.commit(); with create_savepoint that only
        # releases a SAVEPOINT inside `outer`, so the rollback below still undoes it.
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            yield session
        outer.rollback()
