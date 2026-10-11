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

Nightly re-sweep: enqueue_resweep() runs at RESWEEP_AT (local time) and at worker startup.
It queues an ordinary backfill job with {"resweep": true} for the last RESWEEP_DAYS
complete days, which searches them again ignoring coverage. That catches mail Gmail
indexed after the poller's overlap window had already passed it.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from http import HTTPStatus
from typing import Any, Final

from googleapiclient.errors import HttpError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import jobqueue
from app.config import Settings
from app.db.models import Job, JobKind
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

# Re-sweep the two complete days before today: one missed night (laptop off, token
# expired) is still covered by the next one.
RESWEEP_DAYS: Final = 2
RESWEEP_AT: Final = time(3, 0)  # in Settings.tz: quiet hours, yesterday is long finished


class InvalidJobParams(ValueError):
    """The job's params can't describe a backfill; retrying won't change that."""


@dataclass(frozen=True, slots=True)
class BackfillParams:
    days: DateRange
    resweep: bool  # True: search every day again, ignoring (and not adding) coverage


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
        params = parse_params(lease.params)
        source = source_factory(settings)
        with sessions() as session:

            def heartbeat(progress: dict[str, int]) -> bool:
                return jobqueue.heartbeat(session, lease, dict(progress), now=clock())

            result = backfill(
                session,
                source,
                packs,
                params.days,
                zone=settings.zone,
                now=clock(),
                on_progress=heartbeat,
                use_coverage=not params.resweep,
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


def parse_params(params: dict[str, Any]) -> BackfillParams:
    """{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"[, "resweep": true]} (end exclusive)."""
    try:
        days = DateRange(date.fromisoformat(params["start"]), date.fromisoformat(params["end"]))
    except (KeyError, TypeError, ValueError) as e:
        raise InvalidJobParams(f"bad backfill params: {type(e).__name__}") from e
    resweep = params.get("resweep", False)
    if not isinstance(resweep, bool):  # "false" would be truthy: refuse, don't guess
        raise InvalidJobParams("bad backfill params: resweep must be true or false")
    return BackfillParams(days, resweep)


def enqueue_resweep(
    settings: Settings,
    *,
    sessions: sessionmaker[Session] | None = None,
    clock: Clock | None = None,
) -> int | None:
    """Queue the re-sweep of the last RESWEEP_DAYS complete days. Returns the job id, or
    None if that exact re-sweep was queued before (any status), so restarts don't repeat it.
    """
    sessions = sessions or get_sessionmaker()
    clock = clock or _utc_now
    today = clock().astimezone(settings.zone).date()
    days = DateRange(today - timedelta(days=RESWEEP_DAYS), today)
    params = {"start": days.start.isoformat(), "end": days.end.isoformat(), "resweep": True}
    last = days.end - timedelta(days=1)
    with sessions.begin() as session:
        # jsonb equality ignores key order. Two workers starting at once could both insert;
        # harmless (the work is idempotent), so no lock or unique index for it.
        seen = session.scalar(
            select(Job.id).where(Job.kind == JobKind.BACKFILL, Job.params == params).limit(1)
        )
        if seen is not None:
            logger.info("resweep of %s .. %s already queued (job %d)", days.start, last, seen)
            return None
        job_id = jobqueue.enqueue(session, JobKind.BACKFILL, params).id
    logger.info("resweep job %d queued: %s .. %s", job_id, days.start, last)
    return job_id


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
