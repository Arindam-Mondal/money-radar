"""Incremental poll (ING-1): fetch money mail received since the last successful poll.

The cursor is a time watermark, not a Gmail history id: each poll searches
`build_query(after=watermark - overlap)` and moves the watermark to the poll's start time.
The overlap re-reads the last few minutes on purpose (search index lag, clock skew);
mail already recorded is skipped, so the overlap costs one list call, not extra fetches.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import SyncState
from app.pipeline.gmail.ingest import ingest_messages
from app.pipeline.gmail.query import build_query
from app.pipeline.gmail.source import MailSource
from app.regions.base import RegionPack

# First poll ever: look back this far. Anything older is the backfill's job (ING-2).
FIRST_POLL_LOOKBACK: Final = timedelta(days=1)


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
    counts = ingest_messages(session, source, listed, now=now)

    state.polled_until = now
    session.flush()
    return PollResult(
        searched_after=searched_after,
        listed=len(listed),
        recorded=counts.recorded,
        unverified=counts.unverified,
        failed=counts.failed,
    )


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
