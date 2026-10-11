"""Worker: poll job wiring, heartbeat file, scheduler wiring. No real Gmail, no real signals."""

import logging
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import app.worker.__main__ as worker_main
from app.config import Settings
from app.pipeline.gmail.client import GmailMailSource
from app.pipeline.gmail.poller import PollResult
from app.regions import load_region_packs
from app.worker import jobs
from app.worker.__main__ import build_scheduler
from app.worker.heartbeat import beat, is_fresh
from tests.fakes.mail_source import FakeMailSource

PACKS = load_region_packs(["india"])


def test_poll_wires_settings_into_poll_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeMailSource()
    calls: list[dict[str, Any]] = []

    def fake_poll_once(session: object, source: object, packs: object, **kwargs: Any) -> PollResult:
        calls.append({"source": source, "packs": packs, **kwargs})
        return PollResult(
            searched_after=kwargs["now"], listed=3, recorded=1, unverified=0, failed=1
        )

    monkeypatch.setattr(GmailMailSource, "from_token_file", lambda path: fake)
    monkeypatch.setattr(jobs, "get_sessionmaker", lambda: MagicMock())  # no real DB
    monkeypatch.setattr(jobs, "poll_once", fake_poll_once)
    settings = Settings(gmail_token_file=tmp_path / "token.json", poll_overlap_minutes=7)

    with caplog.at_level(logging.INFO, logger="money_radar.worker"):
        jobs.poll_gmail(settings, PACKS)

    assert calls[0]["source"] is fake
    assert calls[0]["overlap"] == timedelta(minutes=7)
    assert calls[0]["packs"] is PACKS
    assert "listed=3 recorded=1 unverified=0 failed=1" in caplog.text


def test_poll_without_token_logs_and_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(gmail_token_file=tmp_path / "absent.json")
    with caplog.at_level(logging.ERROR, logger="money_radar.worker"):
        jobs.poll_gmail(settings, PACKS)  # real load_credentials -> GmailAuthError, no DB use

    assert "poll skipped: No Gmail token" in caplog.text


def test_unknown_region_pack_fails_at_startup(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown region pack"):
        build_scheduler(Settings(region_packs=("mars",)), tmp_path / "hb")


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


def test_backfill_job_polls_at_the_configured_interval(tmp_path: Path) -> None:
    scheduler = build_scheduler(Settings(job_poll_seconds=7), tmp_path / "hb")

    job = scheduler.get_job("run_backfills")

    assert job.trigger.interval == timedelta(seconds=7)
    assert job.max_instances == 1  # ticks are skipped while a backfill runs


def test_skipped_backfill_ticks_are_not_logged() -> None:
    quiet = worker_main._QuietSkippedBackfillTicks()

    def record(message: str) -> logging.LogRecord:
        return logging.LogRecord("apscheduler.scheduler", logging.WARNING, "", 0, message, (), None)

    tail = "skipped: maximum number of running instances reached (1)"
    skipped = f'Execution of job "run_backfills (trigger: interval[0:00:30])" {tail}'
    other = f'Execution of job "poll_gmail (trigger: interval[0:02:00])" {tail}'
    assert not quiet.filter(record(skipped))
    assert quiet.filter(record(other))  # a stuck Gmail poll is still worth a warning


def test_scheduler_runs_first_tick_immediately_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real scheduler in a thread: first tick (no token -> logged error) must still beat.
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(worker_main, "run_backfills", lambda *args: 0)  # no DB in unit tests
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
