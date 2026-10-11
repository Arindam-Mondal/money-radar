"""Shared by poll and backfill: fetch, auth-check and record a batch of Gmail ids (ING-4/5).

Poll and backfill differ only in WHICH ids they ask for; what happens to each id must be
identical, or the two would disagree about the same message (and Gate 1 would fail).
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.pipeline.auth.dmarc import check_dmarc
from app.pipeline.auth.store import apply_auth_verdict
from app.pipeline.gmail.record import record_failed, record_message, unseen_gmail_ids
from app.pipeline.gmail.source import MailSource, MessageParseError

logger = logging.getLogger("money_radar.pipeline")


@dataclass(frozen=True, slots=True)
class IngestCounts:
    recorded: int = 0  # new messages rows
    unverified: int = 0  # of those, how many failed the DMARC check
    failed: int = 0  # poison messages recorded as `failed` (won't parse, or broke the auth check)

    def __add__(self, other: "IngestCounts") -> "IngestCounts":
        return IngestCounts(
            self.recorded + other.recorded,
            self.unverified + other.unverified,
            self.failed + other.failed,
        )


def ingest_messages(
    session: Session, source: MailSource, gmail_ids: Iterable[str], *, now: datetime
) -> IngestCounts:
    """Record every id not yet in `messages`. Does not commit: the caller owns the transaction.

    Gmail/network errors are NOT caught: they abort the caller and its transaction rolls
    back. Only errors about one message's data are, so it can't block everything after it.
    `now` stands in for received_at when a message's own date is unreadable too.
    """
    recorded = unverified = failed = 0
    for gmail_id in unseen_gmail_ids(session, gmail_ids):
        try:
            message = source.get_message(gmail_id)
        except MessageParseError as e:
            failed += _record_failed(session, gmail_id, e.received_at or now, "parse", e)
            continue
        try:
            verdict = check_dmarc(message)
        except Exception as e:  # pure function: an error here is about this message's data
            failed += _record_failed(session, gmail_id, message.received_at, "auth", e)
            continue
        if not record_message(session, message):
            continue  # recorded by someone else between the pre-check and now
        recorded += 1
        apply_auth_verdict(session, message.id, verdict)
        if not verdict.passed:
            unverified += 1
    return IngestCounts(recorded, unverified, failed)


def _record_failed(
    session: Session, gmail_id: str, received_at: datetime, stage: str, error: Exception
) -> int:
    """Record a poison message as `failed`; 1 if a row was added, else 0 (for counting)."""
    cause = error.__cause__ or error  # MessageParseError wraps the parser's real error
    reason = f"{stage}: {type(cause).__name__}"  # class name only: exception text can quote mail
    logger.warning("message %s recorded as failed (%s)", gmail_id, reason)
    return int(record_failed(session, gmail_id, received_at=received_at, error=reason))
