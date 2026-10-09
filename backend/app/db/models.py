from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import CheckConstraint, DateTime, MetaData, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names so Alembic can find and drop them later.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Every datetime column is timestamptz: stored as UTC, never ambiguous across time zones.
    type_annotation_map: ClassVar[dict[type, Any]] = {datetime: DateTime(timezone=True)}


class SyncState(Base):
    """Single-row cursor for the Gmail incremental poller (ING-1)."""

    __tablename__ = "sync_state"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    # autoincrement=False: no SERIAL sequence; the only valid id is 1.
    id: Mapped[int] = mapped_column(primary_key=True, default=1, autoincrement=False)
    history_id: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
