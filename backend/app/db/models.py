from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, Identity, MetaData, Text, func
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


def pg_enum(enum_class: type[StrEnum], name: str) -> Enum:
    """A Postgres enum type that stores the members' lowercase values, not their names."""
    return Enum(enum_class, name=name, values_callable=lambda e: [member.value for member in e])


class SyncState(Base):
    """Single-row cursor for the Gmail incremental poller (ING-1)."""

    __tablename__ = "sync_state"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    # autoincrement=False: no SERIAL sequence; the only valid id is 1.
    id: Mapped[int] = mapped_column(primary_key=True, default=1, autoincrement=False)
    history_id: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class MessageStatus(StrEnum):
    """Where a Gmail message is in the pipeline (design.md §4.1 state diagram)."""

    FETCHED = "fetched"  # recorded; next: auth check, masking, classification
    UNVERIFIED = "unverified"  # DMARC did not pass (ING-5); never processed further
    NOT_TXN = "not_txn"  # classifier: not a transaction
    PENDING_CLASSIFICATION = "pending_classification"  # classifier down; retry job picks up
    EXTRACTED = "extracted"  # parsed, reconciled, stored as a transaction
    NEEDS_REVIEW = "needs_review"  # new sender, guardrail failure, low confidence
    FAILED = "failed"  # unexpected error; only an explicit reprocess retries it


class Message(Base):
    """One row per Gmail message we've seen. Metadata only: bodies are never stored."""

    __tablename__ = "messages"

    # GENERATED ALWAYS AS IDENTITY: the SQL-standard successor to bigserial.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # The idempotency key (ING-4): a message is recorded once, however often it's fetched.
    gmail_id: Mapped[str] = mapped_column(Text, unique=True)
    received_at: Mapped[datetime] = mapped_column(index=True)  # Gmail internalDate
    sender_domain: Mapped[str | None] = mapped_column(Text)  # set by the auth check (1.5)
    auth_result: Mapped[str | None] = mapped_column(Text)  # set by the auth check (1.5)
    status: Mapped[MessageStatus] = mapped_column(
        pg_enum(MessageStatus, "message_status"),
        default=MessageStatus.FETCHED,
        server_default=MessageStatus.FETCHED.value,
        index=True,
    )
    pipeline_version: Mapped[int]
    error: Mapped[str | None] = mapped_column(Text)  # short reason for failed/needs_review
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class SenderStatus(StrEnum):
    """Trust level of a sender domain (CLS-3). Set by classification (P3) and the user."""

    LEARNED = "learned"  # seen sending transactions; first message held for review
    APPROVED = "approved"  # user confirmed it's really their bank: skip new-sender review
    BLOCKED = "blocked"  # user rejected it (e.g. lookalike domain): its mail is dropped


class SenderRegistry(Base):
    """Known sender domains, so a lookalike domain that passes DMARC still gets reviewed."""

    __tablename__ = "sender_registry"
    # Lowercase only: otherwise "HDFCBank.net" and "hdfcbank.net" could be two rows.
    __table_args__ = (CheckConstraint("domain = lower(domain)", name="domain_lowercase"),)

    domain: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[SenderStatus] = mapped_column(pg_enum(SenderStatus, "sender_status"))
    first_seen: Mapped[datetime] = mapped_column(server_default=func.now())
    txn_count: Mapped[int] = mapped_column(default=0, server_default="0")
