"""Scheduled jobs. Each one is a plain function: the scheduler only decides WHEN it runs."""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.db.session import get_sessionmaker
from app.pipeline.gmail.auth import GmailAuthError
from app.pipeline.gmail.client import GmailMailSource
from app.pipeline.gmail.poller import poll_once
from app.regions import RegionPack

logger = logging.getLogger("money_radar.worker")


def poll_gmail(settings: Settings, packs: Sequence[RegionPack]) -> None:
    """One poll tick: record new money mail since the watermark (see pipeline/gmail/poller)."""
    now = datetime.now(UTC)  # before the search, so mail arriving mid-poll lands next window
    try:
        # A fresh client per tick: the token may have been refreshed, and the client
        # (httplib2) isn't thread-safe while the scheduler runs jobs in a thread pool.
        source = GmailMailSource.from_token_file(settings.gmail_token_file)
    except GmailAuthError as e:
        # Needs a human (re-run consent). Retrying won't help, crashing would restart-loop:
        # log one clear line per tick and keep the scheduler alive.
        logger.error("poll skipped: %s", e)
        return
    # begin(): commit if the block finishes, roll back if anything raises.
    with get_sessionmaker().begin() as session:
        result = poll_once(
            session,
            source,
            packs,
            now=now,
            overlap=timedelta(minutes=settings.poll_overlap_minutes),
        )
    # Counts only: never log subjects, senders or bodies.
    logger.info(
        "poll ok: after=%s listed=%d recorded=%d unverified=%d failed=%d",
        result.searched_after.isoformat(timespec="seconds"),
        result.listed,
        result.recorded,
        result.unverified,
        result.failed,
    )
