"""Incremental poll (ING-1): fetch money mail received since the last successful poll.

The cursor is a time watermark, not a Gmail history id: each poll searches
`build_query(after=watermark - overlap)` and moves the watermark to the poll's start time.
The overlap re-reads the last few minutes on purpose (search index lag, clock skew);
mail already recorded is skipped, so the overlap costs one list call, not extra fetches.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import SyncState
from app.pipeline.auth.dmarc import check_dmarc
from app.pipeline.auth.store import apply_auth_verdict
from app.pipeline.gmail.query import build_query
from app.pipeline.gmail.record import record_failed, record_message, unseen_gmail_ids
from app.pipeline.gmail.source import MailSource, MessageParseError
from app.regions.base import RegionPack

# First poll ever: look back this far. Anything older is the backfill's job (ING-2).
FIRST_POLL_LOOKBACK: Final = timedelta(days=1)

logger = logging.getLogger("money_radar.pipeline")


@dataclass(frozen=True, slots=True)
class PollResult:
    searched_after: datetime  # start of the search window (watermark - overlap)
    listed: int  # ids the search returned, including ones already recorded
    recorded: int  # new messages rows
    unverified: int  # of those, how many failed the DMARC check
    failed: int  # poison messages recorded as `failed` (won't parse, or broke the auth check)


def poll_once(
    session: Session,
    source: MailSource,
    packs: Iterable[RegionPack],
    *,
    now: datetime,
    overlap: timedelta,
) -> PollResult:
    """Record new money mail and advance the watermark to `now`, in the caller's transaction.

    `now` must be taken BEFORE the search (mail arriving mid-poll is then inside the next
    window). Does not commit: messages, auth verdicts and the new watermark must commit
    together, so a crash part-way leaves the watermark where it was and the next poll
    redoes the same window (at-least-once; recording is idempotent).
    """
    state = _lock_state(session)
    since = state.polled_until or now - FIRST_POLL_LOOKBACK
    searched_after = since - overlap

    listed = list(source.iter_message_ids(build_query(packs, after=searched_after)))
    recorded = unverified = failed = 0
    for gmail_id in unseen_gmail_ids(session, listed):
        # Gmail/network errors are NOT caught: they abort the poll and the next tick retries.
        # Only errors about this one message's data are, so it can't block every later poll.
        try:
            message = source.get_message(gmail_id)
        except MessageParseError as e:
            # internalDate unreadable too (very rare): file it under the poll time instead.
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

    state.polled_until = now
    session.flush()
    return PollResult(
        searched_after=searched_after,
        listed=len(listed),
        recorded=recorded,
        unverified=unverified,
        failed=failed,
    )


def _record_failed(
    session: Session, gmail_id: str, received_at: datetime, stage: str, error: Exception
) -> int:
    """Record a poison message as `failed`; 1 if a row was added, else 0 (for counting)."""
    cause = error.__cause__ or error  # MessageParseError wraps the parser's real error
    reason = f"{stage}: {type(cause).__name__}"  # class name only: exception text can quote mail
    logger.warning("message %s recorded as failed (%s)", gmail_id, reason)
    return int(record_failed(session, gmail_id, received_at=received_at, error=reason))


def _lock_state(session: Session) -> SyncState:
    """The sync_state row, created if missing, locked until the transaction ends.

    The lock makes polls mutually exclusive across processes (APScheduler's max_instances
    only covers one process): a second poller waits here, then sees the moved watermark.
    """
    session.execute(insert(SyncState).values(id=1).on_conflict_do_nothing())
    state = session.get(SyncState, 1, with_for_update=True, populate_existing=True)
    if state is None:  # impossible after the insert above; keeps mypy and readers honest
        raise RuntimeError("sync_state row missing")
    return state
