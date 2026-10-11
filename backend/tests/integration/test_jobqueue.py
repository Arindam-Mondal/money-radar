"""jobqueue against real Postgres: claim order, SKIP LOCKED, leases, poison jobs, lost leases."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, delete, select, text
from sqlalchemy.orm import Session

from app import jobqueue
from app.db.models import Job, JobKind, JobStatus
from app.jobqueue import LEASE, MAX_ATTEMPTS, RETRY_DELAYS

pytestmark = pytest.mark.integration

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
BACKFILL = JobKind.BACKFILL
PARAMS = {"start": "2026-01-01", "end": "2026-02-01"}


@pytest.fixture(autouse=True)
def _empty_queue(session: Session) -> Iterator[None]:
    # Ignore jobs already in the dev DB (rolled back with the test).
    session.execute(delete(Job))
    session.flush()
    yield


def _enqueue(session: Session, params: dict[str, Any] = PARAMS) -> Job:
    return jobqueue.enqueue(session, BACKFILL, params, run_after=NOW)  # due at the test clock


def _job(session: Session, job_id: int) -> Job:
    return session.scalars(select(Job).where(Job.id == job_id)).one()


def test_enqueue_then_claim(session: Session) -> None:
    job = _enqueue(session)

    lease = jobqueue.claim(session, BACKFILL, now=NOW)

    assert lease is not None
    assert (lease.job_id, lease.attempt, lease.params) == (job.id, 1, PARAMS)
    row = _job(session, job.id)
    assert (row.status, row.started_at, row.heartbeat_at) == (JobStatus.RUNNING, NOW, NOW)
    assert jobqueue.claim(session, BACKFILL, now=NOW) is None  # nothing else queued


def test_oldest_job_first(session: Session) -> None:
    first = _enqueue(session, {"n": 1})
    _enqueue(session, {"n": 2})

    lease = jobqueue.claim(session, BACKFILL, now=NOW)

    assert lease is not None and lease.job_id == first.id


def test_progress_renews_the_lease_and_complete_finishes(session: Session) -> None:
    _enqueue(session)
    lease = jobqueue.claim(session, BACKFILL, now=NOW)
    assert lease is not None
    later = NOW + timedelta(minutes=5)

    assert jobqueue.heartbeat(session, lease, {"recorded": 100}, now=later)
    assert jobqueue.complete(session, lease, now=later)

    row = _job(session, lease.job_id)
    assert (row.status, row.progress) == (JobStatus.DONE, {"recorded": 100})
    assert row.heartbeat_at == row.finished_at == later


def test_fail_keeps_a_short_reason(session: Session) -> None:
    _enqueue(session)
    lease = jobqueue.claim(session, BACKFILL, now=NOW)
    assert lease is not None

    jobqueue.fail(session, lease, "HttpError", now=NOW)

    row = _job(session, lease.job_id)
    assert (row.status, row.error) == (JobStatus.FAILED, "HttpError")


def test_a_running_job_is_not_claimed_while_its_lease_is_fresh(session: Session) -> None:
    _enqueue(session)
    jobqueue.claim(session, BACKFILL, now=NOW)

    assert jobqueue.claim(session, BACKFILL, now=NOW + LEASE - timedelta(seconds=1)) is None


def test_an_orphaned_job_is_reclaimed_after_the_lease(session: Session) -> None:
    _enqueue(session)
    first = jobqueue.claim(session, BACKFILL, now=NOW)  # ... and then the worker dies

    second = jobqueue.claim(session, BACKFILL, now=NOW + LEASE + timedelta(seconds=1))

    assert first is not None and second is not None
    assert second.job_id == first.job_id
    assert second.attempt == 2


def test_the_old_worker_learns_it_lost_the_lease(session: Session) -> None:
    _enqueue(session)
    stale = jobqueue.claim(session, BACKFILL, now=NOW)
    later = NOW + LEASE + timedelta(seconds=1)
    fresh = jobqueue.claim(session, BACKFILL, now=later)  # the old worker was only slow
    assert stale is not None and fresh is not None

    # The slow worker comes back: every write is refused, so it knows to stop.
    assert not jobqueue.heartbeat(session, stale, {"recorded": 1}, now=later)
    assert not jobqueue.complete(session, stale, now=later)
    assert jobqueue.heartbeat(session, fresh, {"recorded": 2}, now=later)
    assert _job(session, fresh.job_id).progress == {"recorded": 2}


def test_a_job_that_keeps_dying_is_failed_not_retried_forever(session: Session) -> None:
    job = _enqueue(session)
    moment = NOW
    for _ in range(MAX_ATTEMPTS):  # claimed, then the worker dies, each time
        assert jobqueue.claim(session, BACKFILL, now=moment) is not None
        moment += LEASE + timedelta(seconds=1)

    assert jobqueue.claim(session, BACKFILL, now=moment) is None

    row = _job(session, job.id)
    assert row.status is JobStatus.FAILED
    assert row.error == f"gave up after {MAX_ATTEMPTS} attempts"


def test_a_queued_job_waits_for_run_after(session: Session) -> None:
    job = jobqueue.enqueue(session, BACKFILL, PARAMS, run_after=NOW + timedelta(minutes=5))

    assert jobqueue.claim(session, BACKFILL, now=NOW) is None  # not due yet
    lease = jobqueue.claim(session, BACKFILL, now=NOW + timedelta(minutes=5))
    assert lease is not None and lease.job_id == job.id


def test_retry_requeues_with_growing_delays(session: Session) -> None:
    _enqueue(session)
    moment = NOW
    for attempt in range(1, MAX_ATTEMPTS):
        lease = jobqueue.claim(session, BACKFILL, now=moment)
        assert lease is not None and lease.attempt == attempt

        assert jobqueue.retry(session, lease, "HttpError", now=moment)

        row = _job(session, lease.job_id)
        assert (row.status, row.error) == (JobStatus.QUEUED, "HttpError")
        assert row.run_after == moment + RETRY_DELAYS[attempt - 1]
        assert row.heartbeat_at is None
        assert jobqueue.claim(session, BACKFILL, now=row.run_after - timedelta(seconds=1)) is None
        moment = row.run_after  # next attempt exactly when due


def test_retry_on_the_last_attempt_fails_the_job(session: Session) -> None:
    job = _enqueue(session)
    moment = NOW
    for _ in range(MAX_ATTEMPTS):
        lease = jobqueue.claim(session, BACKFILL, now=moment)
        assert lease is not None
        jobqueue.retry(session, lease, "HttpError", now=moment)
        moment = _job(session, lease.job_id).run_after

    row = _job(session, job.id)
    assert (row.status, row.error, row.attempts) == (JobStatus.FAILED, "HttpError", MAX_ATTEMPTS)


def test_retry_from_a_lost_lease_is_refused(session: Session) -> None:
    _enqueue(session)
    stale = jobqueue.claim(session, BACKFILL, now=NOW)
    later = NOW + LEASE + timedelta(seconds=1)
    fresh = jobqueue.claim(session, BACKFILL, now=later)
    assert stale is not None and fresh is not None

    assert not jobqueue.retry(session, stale, "HttpError", now=later)
    assert _job(session, fresh.job_id).status is JobStatus.RUNNING  # fresh owner unaffected


def test_complete_after_a_retry_clears_the_old_error(session: Session) -> None:
    _enqueue(session)
    first = jobqueue.claim(session, BACKFILL, now=NOW)
    assert first is not None
    jobqueue.retry(session, first, "HttpError", now=NOW)
    second = jobqueue.claim(session, BACKFILL, now=NOW + RETRY_DELAYS[0])
    assert second is not None

    jobqueue.complete(session, second, now=NOW + RETRY_DELAYS[0])

    assert _job(session, second.job_id).error is None


# --- Two workers at once: needs COMMITTED jobs, so these use their own connections ---------


@pytest.fixture
def committed_jobs(session: Session) -> Iterator[list[int]]:
    """Two queued jobs other connections can see; removed again afterwards."""
    engine: Engine = session.get_bind().engine
    with Session(engine) as setup:
        ids = [_enqueue(setup, {"n": n}).id for n in (1, 2)]
        setup.commit()
    yield ids
    with Session(engine) as cleanup:
        cleanup.execute(delete(Job).where(Job.id.in_(ids)))
        cleanup.commit()


def test_two_workers_never_get_the_same_job(session: Session, committed_jobs: list[int]) -> None:
    engine: Engine = session.get_bind().engine
    with Session(engine) as worker_a, Session(engine) as worker_b:
        # Without SKIP LOCKED a claim would wait for the other's lock forever: make that a
        # failure, not a hung test run.
        for worker in (worker_a, worker_b):
            worker.execute(text("SET LOCAL lock_timeout = '2s'"))
        a = jobqueue.claim(worker_a, BACKFILL, now=NOW)  # A's transaction still open: row locked
        b = jobqueue.claim(worker_b, BACKFILL, now=NOW)  # skips A's row instead of waiting
        c = jobqueue.claim(worker_b, BACKFILL, now=NOW)  # B already holds the second one

        assert a is not None and b is not None
        assert {a.job_id, b.job_id} == set(committed_jobs)
        assert c is None


def test_claim_uses_the_partial_index_with_many_finished_jobs(session: Session) -> None:
    # 20k finished jobs and one queued: the claim must read the small partial index of
    # active rows, not walk the finished ones. Inserted in the rolled-back test transaction.
    session.execute(
        text(
            "INSERT INTO jobs (kind, params, status) "
            "SELECT 'backfill', '{}'::jsonb, 'done' FROM generate_series(1, 20000)"
        )
    )
    _enqueue(session)
    session.execute(text("ANALYZE jobs"))  # fresh statistics, as autovacuum would provide

    plan = "\n".join(
        session.scalars(
            text(
                "EXPLAIN SELECT id FROM jobs WHERE kind = 'backfill' "
                "AND (status = 'queued' AND run_after <= now() "
                "OR status = 'running' AND heartbeat_at < now() - interval '10 minutes') "
                "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
        )
    )

    assert "ix_jobs_claimable" in plan, plan
