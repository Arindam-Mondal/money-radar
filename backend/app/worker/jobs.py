"""Scheduled jobs. Each one is a plain function: the scheduler only decides WHEN it runs."""

import logging
from pathlib import Path

from app.pipeline.gmail.auth import GmailAuthError
from app.pipeline.gmail.client import GmailMailSource

logger = logging.getLogger("money_radar.worker")


def poll_gmail(token_path: Path) -> None:
    """One poll tick. For now it only proves the worker can reach Gmail (token load/refresh
    + one API call); step 1.7 replaces the body with the history-ID incremental fetch."""
    try:
        # A fresh client per tick: the token may have been refreshed, and the client
        # (httplib2) isn't thread-safe while the scheduler runs jobs in a thread pool.
        source = GmailMailSource.from_token_file(token_path)
        history_id = source.current_history_id()
    except GmailAuthError as e:
        # Needs a human (re-run consent). Retrying won't help, crashing would restart-loop:
        # log one clear line per tick and keep the scheduler alive.
        logger.error("poll skipped: %s", e)
        return
    logger.info("poll tick ok: history_id=%s", history_id)
