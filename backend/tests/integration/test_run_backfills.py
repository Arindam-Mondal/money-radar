"""run_backfills against real Postgres + FakeMailSource: the job lifecycle end to end."""

import dataclasses
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httplib2
import pytest
from googleapiclient.errors import HttpError
from sqlalchemy import Connection, delete, select
from sqlalchemy.orm import Session, sessionmaker

from app import jobqueue
from app.config import Settings
from app.db.models import Job, JobKind, JobStatus, Message, SyncRange
from app.jobqueue import RETRY_DELAYS
from app.pipeline.gmail import backfill as backfill_module
from app.pipeline.gmail.auth import GmailAuthError
from app.pipeline.gmail.source import MailMessage, MailSource
from app.regions import load_region_packs
from app.worker.backfills import run_backfills
from tests.fakes.mail_source import FakeMailSource, make_message

pytestmark = pytest.mark.integration

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)  # "today" for the backfill is 2026-03-15
PACKS = load_region_packs(["india"])
JAN = {"start": "2026-01-01", "end": "2026-02-01"}
DMARC_PASS = ("Authentication-Results", "mx.google.com; dmarc=pass header.from=bank.example")


@pytest.fixture(autouse=True)
def _clean(session: Session) -> Iterator[None]:
    # The dev DB may hold real jobs and coverage; hide them (rolled back afterwards).
    session.execute(delete(Job))
    session.execute(delete(SyncRange))
    session.flush()
    yield


@pytest.fixture
def sessions(session: Session) -> sessionmaker[Session]:
    """Sessions on the test's connection: their commits are savepoints, all rolled back."""
    connection = session.get_bind()
    assert isinstance(connection, Connection)
    return sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


def _mail(gmail_id: str, day: date) -> MailMessage:
    message = make_message(gmail_id, datetime(day.year, day.month, day.day, 12, tzinfo=IST))
    return dataclasses.replace(message, headers=(DMARC_PASS, *message.headers))


def _run(sessions: sessionmaker[Session], source: MailSource, *, clock: datetime = NOW) -> int:
    return run_backfills(
        Settings(), PACKS, sessions=sessions, source_factory=lambda _: source, clock=lambda: clock
    )


def _enqueue(session: Session, params: dict[str, Any] = JAN) -> int:
    job = jobqueue.enqueue(session, JobKind.BACKFILL, params, run_after=NOW)
    session.flush()
    return job.id


def _job(session: Session, job_id: int) -> Job:
    session.expire_all()  # see what the worker's sessions committed
    return session.scalars(select(Job).where(Job.id == job_id)).one()


def _recorded(session: Session, prefix: str) -> int:
    rows = session.scalars(select(Message.gmail_id).where(Message.gmail_id.like(f"{prefix}%")))
    return len(list(rows))


class _DiesAfter(FakeMailSource):
    """Raises `error` once `limit` messages have been downloaded (None = never)."""

    def __init__(self, error: Exception, limit: int | None) -> None:
        super().__init__()
        self.error = error
        self.limit = limit

    def get_message(self, message_id: str) -> MailMessage:
        if self.limit is not None and len(self.fetched) >= self.limit:
            raise self.error
        return super().get_message(message_id)


def _http_error(status: int, reason: str | None = None) -> HttpError:
    body = {"error": {"message": "x", "errors": [{"reason": reason}]}} if reason else {}
    return HttpError(httplib2.Response({"status": str(status)}), json.dumps(body).encode())


# --- happy path --------------------------------------------------------------------------


def test_nothing_queued_does_nothing(sessions: sessionmaker[Session]) -> None:
    assert _run(sessions, FakeMailSource()) == 0


def test_a_queued_backfill_runs_to_done(session: Session, sessions: sessionmaker[Session]) -> None:
    job_id = _enqueue(session)
    source = FakeMailSource()
    source.add(*(_mail(f"rb-jan-{d}", date(2026, 1, d)) for d in (3, 15, 28)))

    assert _run(sessions, source) == 1

    job = _job(session, job_id)
    assert (job.status, job.attempts, job.error) == (JobStatus.DONE, 1, None)
    assert job.progress == {
        "gaps_total": 1,
        "gaps_done": 1,
        "listed": 3,
        "recorded": 3,
        "unverified": 0,
        "failed": 0,
    }
    assert _recorded(session, "rb-jan-") == 3
    coverage = session.scalars(select(SyncRange)).one()
    assert (coverage.start_date, coverage.end_date) == (date(2026, 1, 1), date(2026, 2, 1))


def test_one_run_drains_every_ready_job_and_overlap_is_cheap(
    session: Session, sessions: sessionmaker[Session]
) -> None:
    first = _enqueue(session)
    second = _enqueue(session, {"start": "2026-01-15", "end": "2026-02-10"})
    source = FakeMailSource()
    source.add(_mail("rb-a", date(2026, 1, 20)), _mail("rb-b", date(2026, 2, 5)))

    assert _run(sessions, source) == 2

    assert _job(session, first).status is JobStatus.DONE
    later = _job(session, second)
    assert later.status is JobStatus.DONE
    assert later.progress["recorded"] == 1  # only Feb 1-9 was searched: rb-b
    assert source.fetched == ["rb-a", "rb-b"]  # rb-a downloaded once, by the first job


def test_a_job_with_nothing_left_to_fetch_still_reports_progress(
    session: Session, sessions: sessionmaker[Session]
) -> None:
    session.add(SyncRange(start_date=date(2026, 1, 1), end_date=date(2026, 2, 1)))
    job_id = _enqueue(session)

    _run(sessions, FakeMailSource())

    job = _job(session, job_id)
    assert job.status is JobStatus.DONE
    assert (job.progress["gaps_total"], job.progress["listed"]) == (0, 0)  # not just {}


def test_a_job_not_yet_due_is_left_alone(session: Session, sessions: sessionmaker[Session]) -> None:
    job = jobqueue.enqueue(session, JobKind.BACKFILL, JAN, run_after=NOW + timedelta(minutes=1))
    session.flush()

    assert _run(sessions, FakeMailSource()) == 0
    assert _job(session, job.id).status is JobStatus.QUEUED


# --- temporary errors: retry, then resume ------------------------------------------------


def test_temporary_error_requeues_and_the_retry_resumes(
    session: Session, sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backfill_module, "BATCH_SIZE", 2)
    job_id = _enqueue(session)
    source = _DiesAfter(ConnectionError("network down"), limit=3)
    source.add(*(_mail(f"rb-retry-{d:02d}", date(2026, 1, d)) for d in range(1, 6)))

    _run(sessions, source)

    job = _job(session, job_id)
    assert (job.status, job.attempts, job.error) == (JobStatus.QUEUED, 1, "ConnectionError")
    assert job.run_after == NOW + RETRY_DELAYS[0]
    assert job.progress["recorded"] == 2  # batch 1 and its progress committed together
    assert _recorded(session, "rb-retry-") == 2
    assert session.scalars(select(SyncRange)).first() is None  # gap not finished

    assert _run(sessions, source, clock=NOW + timedelta(seconds=30)) == 0  # not due yet

    source.limit = None
    assert _run(sessions, source, clock=NOW + RETRY_DELAYS[0]) == 1

    job = _job(session, job_id)
    assert (job.status, job.attempts, job.error) == (JobStatus.DONE, 2, None)
    assert job.progress["recorded"] == 3  # this attempt: the 3 not saved the first time
    assert _recorded(session, "rb-retry-") == 5


@pytest.mark.parametrize(
    "error",
    [_http_error(503), _http_error(429), _http_error(403, "rateLimitExceeded")],
    ids=["5xx", "429", "rate-limit-403"],
)
def test_gmail_errors_that_pass_with_time_are_retried(
    session: Session, sessions: sessionmaker[Session], error: HttpError
) -> None:
    job_id = _enqueue(session)
    source = _DiesAfter(error, limit=0)
    source.add(_mail("rb-http", date(2026, 1, 9)))

    _run(sessions, source)

    job = _job(session, job_id)
    assert job.status is JobStatus.QUEUED
    assert job.error == f"HttpError {error.resp.status}"


# --- permanent errors: fail at once --------------------------------------------------------


def test_expired_token_fails_at_once(session: Session, sessions: sessionmaker[Session]) -> None:
    job_id = _enqueue(session)

    def no_token(_: Settings) -> MailSource:
        raise GmailAuthError("Gmail token revoked or expired; re-run consent")

    run_backfills(Settings(), PACKS, sessions=sessions, source_factory=no_token, clock=lambda: NOW)

    job = _job(session, job_id)
    assert (job.status, job.attempts, job.error) == (JobStatus.FAILED, 1, "GmailAuthError")


@pytest.mark.parametrize("params", [{"start": "2026-01-01"}, {"start": "soon", "end": "later"}])
def test_bad_params_fail_at_once(
    session: Session, sessions: sessionmaker[Session], params: dict[str, Any]
) -> None:
    job_id = _enqueue(session, params)

    _run(sessions, FakeMailSource())

    job = _job(session, job_id)
    assert (job.status, job.error) == (JobStatus.FAILED, "InvalidJobParams")


def test_a_gmail_4xx_is_permanent(session: Session, sessions: sessionmaker[Session]) -> None:
    job_id = _enqueue(session)
    source = _DiesAfter(_http_error(400), limit=0)
    source.add(_mail("rb-400", date(2026, 1, 9)))

    _run(sessions, source)

    job = _job(session, job_id)
    assert (job.status, job.error) == (JobStatus.FAILED, "HttpError 400")


# --- lost lease: stop, write nothing ------------------------------------------------------------


def test_a_worker_that_lost_its_lease_stops_without_writing(
    session: Session, sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fencing itself (the attempts check) is tested in test_jobqueue.py; here: the worker's
    # reaction when its heartbeat is refused because another worker took the job over.
    job_id = _enqueue(session)
    monkeypatch.setattr(jobqueue, "heartbeat", lambda *args, **kwargs: False)
    source = FakeMailSource()
    source.add(_mail("rb-lost", date(2026, 1, 9)))

    _run(sessions, source)

    job = _job(session, job_id)
    assert (job.status, job.progress, job.finished_at) == (JobStatus.RUNNING, {}, None)
    assert _recorded(session, "rb-lost") == 0  # the batch was rolled back, not committed
    assert session.scalars(select(SyncRange)).first() is None
