"""Backfill API (ING-2, UI-5): queue a backfill and follow its progress.

The API never talks to Gmail. POST only writes a `jobs` row (one transaction) and answers
202 Accepted; the worker's run_backfills job picks it up within JOB_POLL_SECONDS.
"""

from datetime import datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jobqueue
from app.api.schemas import BackfillJob, BackfillRequest
from app.config import Settings, get_settings
from app.db.models import Job, JobKind
from app.db.session import get_session

router = APIRouter(tags=["backfill"])

# Protects the Gmail quota from a typo like 2016 for 2026: ~3 years per request.
MAX_DAYS: Final = 3 * 366


@router.post("/backfill", status_code=status.HTTP_202_ACCEPTED)
def create_backfill(
    body: BackfillRequest,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BackfillJob:
    """Queue a backfill of the calendar days `start`..`end` (both inclusive, in TZ)."""
    today = datetime.now(settings.zone).date()
    if body.start >= today:
        _reject("start must be before today: today's mail is ingested by the poller")
    if body.end > today:
        _reject("end is in the future")
    if (body.end - body.start).days + 1 > MAX_DAYS:
        _reject(f"at most {MAX_DAYS} days per request")

    # Stored half-open, like DateRange and sync_ranges: end is the first day NOT included.
    exclusive_end = body.end + timedelta(days=1)
    params = {"start": body.start.isoformat(), "end": exclusive_end.isoformat()}
    job = jobqueue.enqueue(session, JobKind.BACKFILL, params)
    session.commit()
    session.refresh(job)  # server-side defaults (status, run_after, created_at)

    response.headers["Location"] = f"/backfill/{job.id}"
    return BackfillJob.from_job(job)


@router.get("/backfill/{job_id}")
def get_backfill(job_id: int, session: Annotated[Session, Depends(get_session)]) -> BackfillJob:
    job = session.scalars(
        select(Job).where(Job.id == job_id, Job.kind == JobKind.BACKFILL)
    ).one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no such backfill job")
    return BackfillJob.from_job(job)


def _reject(reason: str) -> None:
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=reason)
