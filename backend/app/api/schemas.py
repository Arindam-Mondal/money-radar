from datetime import date, datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from app.db.models import Job, JobStatus

CheckStatus = Literal["ok", "unavailable"]


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: CheckStatus
    checks: dict[str, CheckStatus]


class BackfillRequest(BaseModel):
    """Calendar days to backfill, both ends inclusive (as people say them), in TZ."""

    model_config = ConfigDict(extra="forbid")  # a typo like "stat" is an error, not ignored

    start: date
    end: date

    @model_validator(mode="after")
    def _start_not_after_end(self) -> Self:
        if self.start > self.end:
            raise ValueError("start must not be after end")
        return self


class BackfillProgress(BaseModel):
    """Counts saved by the worker with each batch (zeros until the job starts)."""

    gaps_total: int = 0  # uncovered day ranges this attempt has to fetch
    gaps_done: int = 0
    listed: int = 0  # ids Gmail returned, including ones already recorded
    recorded: int = 0  # new messages rows
    unverified: int = 0
    failed: int = 0  # poison messages recorded as failed


class BackfillJob(BaseModel):
    job_id: int
    status: JobStatus
    start: date  # inclusive
    end: date  # inclusive
    attempts: int
    error: str | None  # short reason (class name), e.g. "HttpError 503", "GmailAuthError"
    progress: BackfillProgress
    created_at: datetime
    run_after: datetime  # a queued job won't start before this (retry backoff)
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_job(cls, job: Job) -> Self:
        exclusive_end = date.fromisoformat(job.params["end"])
        return cls(
            job_id=job.id,
            status=job.status,
            start=date.fromisoformat(job.params["start"]),
            end=exclusive_end - timedelta(days=1),
            attempts=job.attempts,
            error=job.error,
            progress=BackfillProgress.model_validate(job.progress),
            created_at=job.created_at,
            run_after=job.run_after,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )
