"""Worker entrypoint: `python -m app.worker`. Runs the scheduler until SIGTERM/SIGINT."""

import logging
import signal
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.schedulers.blocking import BlockingScheduler

from app.config import Settings, get_settings
from app.regions import load_region_packs
from app.worker.backfills import run_backfills
from app.worker.heartbeat import HEARTBEAT_FILE, beat
from app.worker.jobs import poll_gmail

logger = logging.getLogger("money_radar.worker")


class _QuietSkippedBackfillTicks(logging.Filter):
    """While a backfill runs (minutes), every run_backfills tick is skipped by design
    (max_instances=1). APScheduler warns each time; that's expected, so drop just those."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not ("run_backfills" in message and "maximum number of running instances" in message)


def build_scheduler(settings: Settings, heartbeat_file: Path = HEARTBEAT_FILE) -> BlockingScheduler:
    packs = load_region_packs(settings.region_packs)  # unknown pack: fail at startup, not per tick
    scheduler = BlockingScheduler(timezone=UTC)
    scheduler.add_job(
        poll_gmail,
        "interval",
        seconds=settings.poll_interval_seconds,
        args=[settings, packs],
        id="poll_gmail",
        max_instances=1,  # one poll at a time here; poller's row lock covers other processes
        coalesce=True,  # after a stall, run once, not once per missed tick
        misfire_grace_time=30,  # a run may still start up to 30 s late
        next_run_time=datetime.now(UTC),  # first tick right away, not after one interval
    )
    scheduler.add_job(
        run_backfills,
        "interval",
        seconds=settings.job_poll_seconds,
        args=[settings, packs],
        id="run_backfills",
        max_instances=1,  # one drain loop at a time; it runs every ready job itself
        coalesce=True,
        misfire_grace_time=30,
        next_run_time=datetime.now(UTC),  # pick up queued (or orphaned) jobs at startup
    )

    def _on_job_done(event: JobExecutionEvent) -> None:
        beat(heartbeat_file)  # success or error: either way the scheduler loop is alive

    scheduler.add_listener(_on_job_done, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    return scheduler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")
    logging.getLogger("apscheduler.scheduler").addFilter(_QuietSkippedBackfillTicks())
    settings = get_settings()  # bad config fails here, before anything is scheduled
    scheduler = build_scheduler(settings)

    def _stop(signum: int, frame: FrameType | None) -> None:
        # Docker sends SIGTERM on stop; Ctrl+C sends SIGINT. Python's default for SIGTERM
        # is to die on the spot, so handle it: stop scheduling, wait for a running job.
        logger.info("%s received, shutting down", signal.Signals(signum).name)
        scheduler.shutdown(wait=True)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    logger.info("worker starting: poll every %ss", settings.poll_interval_seconds)
    scheduler.start()  # blocks until shutdown()
    logger.info("worker stopped")


if __name__ == "__main__":
    main()
