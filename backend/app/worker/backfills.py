"""Scheduled job `run_backfills`: run the backfill jobs the API queued (design §4.6, ING-2).

Every JOB_POLL_SECONDS the scheduler calls run_backfills(), which drains the queue: claim
the oldest ready job, run it, record how it ended, and repeat until nothing is ready. A
long backfill keeps the call busy; the scheduler skips its ticks meanwhile.

How a job ends:
- done: complete()
- permanent error (bad params, Gmail token gone, a Gmail 4xx that isn't a rate limit):
  fail() — retrying can't help, a person has to act
- anything else (rate limit, Gmail 5xx, network, database): retry() with backoff, up to
  jobqueue.MAX_ATTEMPTS; the backfill resumes where it stopped
- lease lost (another worker took the job over): stop, write nothing
"""

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from http import HTTPStatus
from typing import Any

from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session, sessionmaker

from app import jobqueue
from app.config import Settings
from app.db.models import JobKind
from app.db.session import get_sessionmaker
from app.pipeline.gmail.auth import GmailAuthError
from app.pipeline.gmail.backfill import BackfillStopped, backfill
from app.pipeline.gmail.client import GmailMailSource, is_rate_limit
from app.pipeline.gmail.ranges import DateRange
from app.pipeline.gmail.source import MailSource
from app.regions import RegionPack

logger = logging.getLogger("money_radar.worker")

SourceFactory = Callable[[Settings], MailSource]
Clock = Callable[[], datetime]


class InvalidJobParams(ValueError):
    """The job's params can't describe a backfill; retrying won't change that."""


def run_backfills(
    settings: Settings,
    packs: Sequence[RegionPack],
    *,
    sessions: sessionmaker[Session] | None = None,
    source_factory: SourceFactory | None = None,
    clock: Clock | None = None,
) -> int:
    """Run every ready backfill job, oldest first. Returns how many were claimed.

    The keyword arguments exist for tests; the scheduler passes only settings and packs.
    """
    sessions = sessions or get_sessionmaker()
    source_factory = source_factory or _gmail_source
    clock = clock or _utc_now
    claimed = 0
    while True:
        with sessions.begin() as session:  # short transaction: releases the row lock
            lease = jobqueue.claim(session, JobKind.BACKFILL, now=clock())
        if lease is None:
            return claimed
        claimed += 1
        _run_one(settings, packs, lease, sessions, source_factory, clock)


def _run_one(
    settings: Settings,
    packs: Sequence[RegionPack],
    lease: jobqueue.Lease,
    sessions: sessionmaker[Session],
    source_factory: SourceFactory,
    clock: Clock,
) -> None:
    logger.info("backfill job %d: attempt %d, params %s", lease.job_id, lease.attempt, lease.params)
    try:
        requested = parse_params(lease.params)
        source = source_factory(settings)
        with sessions() as session:

            def heartbeat(progress: dict[str, int]) -> bool:
                return jobqueue.heartbeat(session, lease, dict(progress), now=clock())

            result = backfill(
                session,
                source,
                packs,
                requested,
                zone=settings.zone,
                now=clock(),
                on_progress=heartbeat,
            )
    except BackfillStopped:
        logger.warning("backfill job %d: lease lost, another worker has it", lease.job_id)
        return
    except Exception as e:
        reason = _reason(e)
        permanent = _is_permanent(e)
        end = jobqueue.fail if permanent else jobqueue.retry
        with sessions.begin() as session:
            ours = end(session, lease, reason, now=clock())
        outcome = "failed" if permanent else "will retry"
        if not ours:
            outcome += ", but the lease was lost so nothing was written"
        logger.warning("backfill job %d: %s (%s)", lease.job_id, outcome, reason)
        return

    with sessions.begin() as session:
        ours = jobqueue.complete(session, lease, now=clock())
    logger.info(
        "backfill job %d: done, gaps=%d listed=%d recorded=%d%s",
        lease.job_id,
        len(result.gaps),
        result.listed,
        result.counts.recorded,
        "" if ours else " (lease lost: status left to the new owner)",
    )


def parse_params(params: dict[str, Any]) -> DateRange:
    """{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} (end exclusive) -> DateRange."""
    try:
        return DateRange(date.fromisoformat(params["start"]), date.fromisoformat(params["end"]))
    except (KeyError, TypeError, ValueError) as e:
        raise InvalidJobParams(f"bad backfill params: {type(e).__name__}") from e


def _is_permanent(error: Exception) -> bool:
    if isinstance(error, InvalidJobParams | GmailAuthError):
        return True
    if isinstance(error, HttpError):
        # 5xx and rate limits pass with time; any other 4xx means the request itself is wrong.
        status = error.resp.status
        temporary = status >= HTTPStatus.INTERNAL_SERVER_ERROR or is_rate_limit(error)
        return not temporary
    return False  # network, database, anything unexpected: retry (bounded by MAX_ATTEMPTS)


def _reason(error: Exception) -> str:
    """Short, content-free reason for jobs.error: class name (+ HTTP status), never the text."""
    if isinstance(error, HttpError):
        return f"HttpError {error.resp.status}"
    return type(error).__name__


def _gmail_source(settings: Settings) -> MailSource:
    return GmailMailSource.from_token_file(settings.gmail_token_file)


def _utc_now() -> datetime:
    return datetime.now(UTC)
