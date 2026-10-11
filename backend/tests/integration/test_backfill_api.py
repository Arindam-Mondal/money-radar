"""POST /backfill and GET /backfill/{id} against real Postgres (rolled back per test)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, delete, select
from sqlalchemy.orm import Session

from app import jobqueue
from app.api.main import create_app
from app.api.routers.backfill import MAX_DAYS
from app.config import get_settings
from app.db.models import Job, JobKind, JobStatus
from app.db.session import get_session

pytestmark = pytest.mark.integration


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    session.execute(delete(Job))  # hide real jobs in the dev DB (rolled back afterwards)
    session.flush()
    connection = session.get_bind()
    assert isinstance(connection, Connection)

    def request_session() -> Iterator[Session]:
        # A fresh session per request, like production: whatever the endpoint doesn't
        # commit is rolled back when the request ends (its commit is a savepoint here).
        with Session(bind=connection, join_transaction_mode="create_savepoint") as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = request_session
    with TestClient(app) as client:
        yield client


def _today() -> date:
    return datetime.now(get_settings().zone).date()


def _days_ago(days: int) -> str:
    return (_today() - timedelta(days=days)).isoformat()


def test_post_queues_a_job_and_points_to_it(client: TestClient, session: Session) -> None:
    response = client.post("/backfill", json={"start": "2026-09-01", "end": "2026-09-30"})

    assert response.status_code == 202
    body = response.json()
    assert response.headers["Location"] == f"/backfill/{body['job_id']}"
    assert (body["status"], body["start"], body["end"]) == ("queued", "2026-09-01", "2026-09-30")
    assert body["progress"]["recorded"] == 0
    job = session.scalars(select(Job).where(Job.id == body["job_id"])).one()
    # Stored half-open, the way the worker and sync_ranges use it.
    assert job.params == {"start": "2026-09-01", "end": "2026-10-01"}


def test_get_shows_status_and_progress(client: TestClient, session: Session) -> None:
    job_id = client.post("/backfill", json={"start": "2026-09-01", "end": "2026-09-30"}).json()[
        "job_id"
    ]
    lease = jobqueue.claim(session, JobKind.BACKFILL, now=datetime.now(UTC))  # "the worker"
    assert lease is not None
    jobqueue.heartbeat(
        session, lease, {"gaps_total": 1, "listed": 100, "recorded": 100}, now=datetime.now(UTC)
    )

    body = client.get(f"/backfill/{job_id}").json()

    assert (body["status"], body["attempts"]) == ("running", 1)
    assert body["progress"] == {
        "gaps_total": 1,
        "gaps_done": 0,
        "listed": 100,
        "recorded": 100,
        "unverified": 0,
        "failed": 0,
    }


def test_get_shows_why_a_job_failed(client: TestClient, session: Session) -> None:
    job_id = client.post("/backfill", json={"start": "2026-09-01", "end": "2026-09-30"}).json()[
        "job_id"
    ]
    lease = jobqueue.claim(session, JobKind.BACKFILL, now=datetime.now(UTC))
    assert lease is not None
    jobqueue.fail(session, lease, "GmailAuthError", now=datetime.now(UTC))

    body = client.get(f"/backfill/{job_id}").json()

    assert (body["status"], body["error"]) == (JobStatus.FAILED.value, "GmailAuthError")
    assert body["finished_at"] is not None


def test_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/backfill/999999999").status_code == 404


def test_up_to_today_is_accepted(client: TestClient) -> None:
    # Today itself is allowed; the worker stops at yesterday (today is the poller's).
    response = client.post("/backfill", json={"start": _days_ago(7), "end": _days_ago(0)})
    assert response.status_code == 202


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"start": "2026-09-30", "end": "2026-09-01"}, "start must not be after end"),
        ({"start": "2026-09-01"}, "Field required"),
        ({"start": "2026-09-01", "end": "2026-09-31"}, "date"),  # no Sep 31
        ({"start": "2026-09-01", "end": "2026-09-30", "stat": "x"}, "Extra inputs"),
    ],
    ids=["reversed", "missing-end", "bad-date", "unknown-field"],
)
def test_malformed_requests_are_422(client: TestClient, body: dict[str, str], reason: str) -> None:
    response = client.post("/backfill", json=body)

    assert response.status_code == 422
    assert reason in response.text


@pytest.mark.parametrize(
    ("start_ago", "end_ago", "reason"),
    [
        (0, 0, "start must be before today"),
        (3, -1, "end is in the future"),
        (MAX_DAYS, 0, f"at most {MAX_DAYS} days"),  # MAX_DAYS + 1 days, inclusive
    ],
    ids=["starts-today", "ends-tomorrow", "too-long"],
)
def test_ranges_the_backfill_cannot_or_should_not_run_are_422(
    client: TestClient, session: Session, start_ago: int, end_ago: int, reason: str
) -> None:
    response = client.post(
        "/backfill", json={"start": _days_ago(start_ago), "end": _days_ago(end_ago)}
    )

    assert response.status_code == 422
    assert reason in response.json()["detail"]
    assert session.scalars(select(Job)).first() is None  # nothing queued
