from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Identity,
    Index,
    MetaData,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
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
    """Single-row watermark for the Gmail incremental poller (ING-1)."""

    __tablename__ = "sync_state"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    # autoincrement=False: no SERIAL sequence; the only valid id is 1.
    id: Mapped[int] = mapped_column(primary_key=True, default=1, autoincrement=False)
    # Start time of the last successful poll; None until the first poll commits.
    polled_until: Mapped[datetime | None]
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


class SyncRange(Base):
    """Calendar days the backfill has fully ingested (ING-2). Half-open: end_date is excluded.

    A row is written only after every message in the range was processed, so an
    interrupted backfill leaves no row and the next run redoes that range.
    """

    __tablename__ = "sync_ranges"
    __table_args__ = (CheckConstraint("start_date < end_date", name="nonempty"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    start_date: Mapped[date]
    end_date: Mapped[date]  # first day NOT covered
    completed_at: Mapped[datetime] = mapped_column(server_default=func.now())


class JobKind(StrEnum):
    """What a queued job does. The API enqueues, the worker runs (design.md §4.6)."""

    BACKFILL = "backfill"  # params: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} (end exclusive)


class JobStatus(StrEnum):
    QUEUED = "queued"  # waiting for a worker
    RUNNING = "running"  # claimed; the worker refreshes heartbeat_at while it works
    DONE = "done"
    FAILED = "failed"  # error says why (class name only)


class Job(Base):
    """Postgres as a work queue: API -> worker. Claimed with FOR UPDATE SKIP LOCKED."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kind: Mapped[JobKind] = mapped_column(pg_enum(JobKind, "job_kind"))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"),
        default=JobStatus.QUEUED,
        server_default=JobStatus.QUEUED.value,
    )
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    run_after: Mapped[datetime] = mapped_column(server_default=func.now())  # retry backoff
    started_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime | None]  # the lease: a RUNNING job with an old one is orphaned
    finished_at: Mapped[datetime | None]

    # The claim reads only active jobs, oldest first: a partial index holds just those
    # rows, so claiming stays fast however many finished jobs pile up.
    __table_args__ = (
        Index(
            "ix_jobs_claimable",
            "kind",
            "id",
            postgresql_where=status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
        ),
    )
