"""Worker liveness without an HTTP server: the scheduler touches a file after every job run,
and the Compose healthcheck (`python -m app.worker.heartbeat`) checks how old it is."""

import sys
import tempfile
import time
from pathlib import Path
from typing import Final

from app.config import get_settings

# /tmp in the container (a tmpfs, since the root fs is read-only). Per-container, not shared.
HEARTBEAT_FILE: Final = Path(tempfile.gettempdir()) / "money-radar-worker.heartbeat"
# Unhealthy only after this many poll intervals pass without any job finishing.
_MISSED_TICKS: Final = 3


def beat(path: Path = HEARTBEAT_FILE) -> None:
    path.touch()  # creates the file or bumps its mtime; the content doesn't matter


def is_fresh(path: Path, max_age_seconds: float, *, now: float | None = None) -> bool:
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return False  # no job has finished yet
    return (time.time() if now is None else now) - mtime <= max_age_seconds


def main() -> None:
    max_age = _MISSED_TICKS * get_settings().poll_interval_seconds
    sys.exit(0 if is_fresh(HEARTBEAT_FILE, max_age) else 1)


if __name__ == "__main__":
    main()
