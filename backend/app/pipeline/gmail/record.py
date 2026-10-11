"""Record stage (ING-4): one `messages` row per Gmail message, however often it's fetched."""

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Message, MessageStatus
from app.pipeline import PIPELINE_VERSION
from app.pipeline.gmail.source import MailMessage


def record_message(
    session: Session, message: MailMessage, *, pipeline_version: int = PIPELINE_VERSION
) -> bool:
    """Insert a `fetched` row for this message. Returns False if it was already recorded.

    Idempotent and race-safe: the database's unique constraint on gmail_id decides, so two
    workers (or a poll and a backfill) recording the same message can't both insert it.
    Does not commit: the caller owns the transaction.
    """
    return _insert(
        session,
        gmail_id=message.id,
        received_at=message.received_at,
        status=MessageStatus.FETCHED,
        pipeline_version=pipeline_version,
    )


def record_failed(
    session: Session,
    gmail_id: str,
    *,
    received_at: datetime,
    error: str,
    pipeline_version: int = PIPELINE_VERSION,
) -> bool:
    """Insert a `failed` row for a message that can't be processed. False if already recorded.

    The row keeps a poison message visible (and re-runnable by an explicit reprocess)
    instead of letting it vanish behind the poll watermark. `error` is a short reason
    such as "parse: ValueError": class names only, never exception text, which can
    quote headers or body. Same idempotency as record_message; does not commit.
    """
    return _insert(
        session,
        gmail_id=gmail_id,
        received_at=received_at,
        status=MessageStatus.FAILED,
        error=error,
        pipeline_version=pipeline_version,
    )


def _insert(session: Session, **values: object) -> bool:
    statement = (
        insert(Message)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[Message.gmail_id])
        .returning(Message.id)
    )
    return session.execute(statement).scalar_one_or_none() is not None


def unseen_gmail_ids(session: Session, gmail_ids: Iterable[str]) -> list[str]:
    """The ids not yet in `messages`, in input order, without duplicates.

    Lets backfill/poll skip messages.get for mail we already have (saves Gmail API quota).
    A pre-check only: record_message's ON CONFLICT is still what guarantees uniqueness.
    """
    candidates = list(dict.fromkeys(gmail_ids))  # de-duplicate, keep order
    if not candidates:
        return []
    seen = set(session.scalars(select(Message.gmail_id).where(Message.gmail_id.in_(candidates))))
    return [gmail_id for gmail_id in candidates if gmail_id not in seen]
