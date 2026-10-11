"""Worker: poll job, heartbeat file, scheduler wiring. No real Gmail, no real signals."""

import logging
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from app.config import Settings
from app.pipeline.gmail.client import GmailMailSource
from app.worker.__main__ import build_scheduler
from app.worker.heartbeat import beat, is_fresh
from app.worker.jobs import poll_gmail
from tests.fakes.mail_source import FakeMailSource


def test_poll_logs_history_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeMailSource(start_history_id=4242)
    monkeypatch.setattr(GmailMailSource, "from_token_file", lambda path: fake)

    with caplog.at_level(logging.INFO, logger="money_radar.worker"):
        poll_gmail(tmp_path / "token.json")

    assert "poll tick ok: history_id=4242" in caplog.text


def test_poll_without_token_logs_and_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="money_radar.worker"):
        poll_gmail(tmp_path / "absent.json")  # real load_credentials -> GmailAuthError

    assert "poll skipped: No Gmail token" in caplog.text


def test_heartbeat_missing_file_is_not_fresh(tmp_path: Path) -> None:
    assert not is_fresh(tmp_path / "hb", max_age_seconds=60)


def test_heartbeat_fresh_then_stale(tmp_path: Path) -> None:
    hb = tmp_path / "hb"
    beat(hb)
    mtime = hb.stat().st_mtime

    assert is_fresh(hb, max_age_seconds=60, now=mtime + 59)
    assert not is_fresh(hb, max_age_seconds=60, now=mtime + 61)


def test_scheduler_job_config(tmp_path: Path) -> None:
    scheduler = build_scheduler(Settings(poll_interval_seconds=90), tmp_path / "hb")

    job = scheduler.get_job("poll_gmail")

    assert job.trigger.interval == timedelta(seconds=90)
    assert job.max_instances == 1
    assert job.coalesce is True


def test_scheduler_runs_first_tick_immediately_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real scheduler in a thread: first tick (no token -> logged error) must still beat.
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(tmp_path / "absent.json"))
    hb = tmp_path / "hb"
    scheduler = build_scheduler(Settings(), hb)
    thread = threading.Thread(target=scheduler.start, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not hb.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert hb.exists(), "first tick did not run within 5 s"
    finally:
        scheduler.shutdown(wait=True)
        thread.join(timeout=5)
    assert not thread.is_alive()
