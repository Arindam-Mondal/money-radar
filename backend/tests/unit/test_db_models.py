from sqlalchemy import DateTime, Text
from sqlalchemy.schema import CheckConstraint

from app.db.models import Base
from app.db.session import get_engine


def test_metadata_registers_sync_state() -> None:
    assert "sync_state" in Base.metadata.tables


def test_sync_state_columns() -> None:
    table = Base.metadata.tables["sync_state"]
    assert isinstance(table.c.history_id.type, Text)
    assert table.c.history_id.nullable
    updated_at = table.c.updated_at.type
    assert isinstance(updated_at, DateTime)
    assert updated_at.timezone  # timestamptz, not naive timestamp
    assert table.c.id.autoincrement is False  # no SERIAL sequence on a single-row table


def test_single_row_check_is_named_by_convention() -> None:
    table = Base.metadata.tables["sync_state"]
    checks = [c for c in table.constraints if isinstance(c, CheckConstraint)]
    assert [c.name for c in checks] == ["ck_sync_state_single_row"]


def test_engine_is_lazy_and_targets_psycopg() -> None:
    # Building the engine must not connect; it only needs settings.
    engine = get_engine()
    assert engine.url.drivername == "postgresql+psycopg"
    assert engine.url.host == "db"
