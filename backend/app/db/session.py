from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    # Created on first use, not at import: importing app.db needs no database or password.
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,  # test a pooled connection before use; survives db restarts
        pool_size=5,
        max_overflow=5,
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(get_engine(), expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    with get_sessionmaker()() as session:
        yield session
