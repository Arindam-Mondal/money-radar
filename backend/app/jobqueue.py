"""Job queue on Postgres: the API enqueues work, the worker claims and runs it (design §4.6).

Claiming uses SELECT … FOR UPDATE SKIP LOCKED, so any number of workers can poll the same
table without ever taking the same job. A claimed job holds a LEASE: the worker refreshes
`heartbeat_at` as it makes progress. If the worker dies, the heartbeat goes stale and the
job becomes claimable again (its work is idempotent, so redoing part of it is safe).

Ending a job (the worker decides which, design §4.6):
- complete(): done.
- retry(): a TEMPORARY error (Gmail rate limit or 5xx, a database blip). The job goes back
  to the queue and runs again after a growing delay; after MAX_ATTEMPTS it fails.
- fail(): a PERMANENT error (expired Gmail token, bad params). Retrying cannot help.

None of these functions commit: the caller owns the transaction. Commit right after
claim() so the row lock is released and other workers and the API see the new status.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.db.models import Job, JobKind, JobStatus

# A RUNNING job whose heartbeat is older than this is assumed orphaned (worker died).
# Must be longer than the longest gap between heartbeats: one batch of 100 messages plus
# Gmail rate-limit waits (up to ~3 min) fits comfortably.
LEASE: Final = timedelta(minutes=10)
# A job that has been claimed this many times without finishing is failed, not retried:
# something about it keeps killing the worker (a "poison job").
MAX_ATTEMPTS: Final = 3
# How long a job waits before its next attempt after a temporary error (attempt 1, 2, ...).
RETRY_DELAYS: Final = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15))


@dataclass(frozen=True, slots=True)
class Lease:
    """Proof that this worker holds a job. `attempt` is fixed at claim time on purpose: an ORM
    object would reload `attempts` from the database and could pick up another worker's."""

    job_id: int
    attempt: int
    kind: JobKind
    params: dict[str, Any]


def enqueue(
    session: Session,
    kind: JobKind,
    params: dict[str, Any],
    *,
    run_after: datetime | None = None,
) -> Job:
    """Add a QUEUED job; the id is assigned on flush, so it can be returned to the caller.

    `run_after` delays it (default: the database's now(), i.e. as soon as a worker is free).
    """
    job = Job(kind=kind, params=params)
    if run_after is not None:
        job.run_after = run_after
    session.add(job)
    session.flush()
    return job


def claim(session: Session, kind: JobKind, *, now: datetime) -> Lease | None:
    """Take the oldest available job of `kind`, mark it RUNNING and return its lease (or None).

    Available = QUEUED and due (run_after reached), or RUNNING with an expired lease. SKIP
    LOCKED makes a second worker step over a row another is claiming instead of waiting.
    """
    claimable = or_(
        and_(Job.status == JobStatus.QUEUED, Job.run_after <= now),
        and_(Job.status == JobStatus.RUNNING, Job.heartbeat_at < now - LEASE),
    )
    while True:
        job = session.scalars(
            select(Job)
            .where(Job.kind == kind, claimable)
            .order_by(Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if job is None:
            return None
        if job.attempts >= MAX_ATTEMPTS:
            job.status = JobStatus.FAILED  # we hold its row lock: safe to write directly
            job.error = f"gave up after {job.attempts} attempts"
            job.finished_at = now
            session.flush()
            continue  # look for the next one
        job.status = JobStatus.RUNNING
        job.attempts += 1
        job.started_at = job.started_at or now
        job.heartbeat_at = now
        session.flush()
        return Lease(job.id, job.attempts, job.kind, dict(job.params))


def heartbeat(session: Session, lease: Lease, progress: dict[str, Any], *, now: datetime) -> bool:
    """Save progress and renew the lease. False if the job is no longer ours to run."""
    return _update_if_ours(session, lease, progress=progress, heartbeat_at=now)


def complete(session: Session, lease: Lease, *, now: datetime) -> bool:
    """Mark the job DONE. False if it was no longer ours (lease lost): nothing is written."""
    return _update_if_ours(session, lease, status=JobStatus.DONE, error=None, finished_at=now)


def retry(session: Session, lease: Lease, error: str, *, now: datetime) -> bool:
    """Put the job back in the queue after a temporary error, to run again after a delay.

    On the last attempt this fails the job instead. False if it was no longer ours.
    """
    if lease.attempt >= MAX_ATTEMPTS:
        return fail(session, lease, error, now=now)
    return _update_if_ours(
        session,
        lease,
        status=JobStatus.QUEUED,
        error=error,
        run_after=now + RETRY_DELAYS[lease.attempt - 1],
        heartbeat_at=None,
    )


def fail(session: Session, lease: Lease, error: str, *, now: datetime) -> bool:
    """Mark the job FAILED. `error` is a short reason, class names only (text can quote mail)."""
    return _update_if_ours(session, lease, status=JobStatus.FAILED, error=error, finished_at=now)


def _update_if_ours(session: Session, lease: Lease, **values: object) -> bool:
    """UPDATE the job only while we still hold its lease.

    Matching on the attempt number is what makes this safe: if our lease expired and
    another worker re-claimed the job, attempts went up, this matches no row, and we
    learn that we must stop rather than both workers running (and finishing) the job.
    """
    result = session.execute(
        update(Job)
        .where(
            Job.id == lease.job_id, Job.status == JobStatus.RUNNING, Job.attempts == lease.attempt
        )
        .values(**values)
        .returning(Job.id)
    )
    return result.scalar_one_or_none() is not None
