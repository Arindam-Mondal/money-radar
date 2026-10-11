"""poll_once against real Postgres + FakeMailSource: watermark, overlap, crash, DMARC,
poison messages."""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.models import Message, MessageStatus, SyncState
from app.pipeline.gmail import ingest
from app.pipeline.gmail.poller import FIRST_POLL_LOOKBACK, _lock_state, poll_once
from app.pipeline.gmail.source import MailMessage
from app.regions import load_region_packs
from tests.fakes.mail_source import FakeMailSource, make_message

pytestmark = pytest.mark.integration

NOW = datetime(2026, 3, 1, 8, 17, tzinfo=UTC)
OVERLAP = timedelta(minutes=10)
PACKS = load_region_packs(["india"])
DMARC_PASS = ("Authentication-Results", "mx.google.com; dmarc=pass header.from=bank.example")


def _mail(gmail_id: str, received_at: datetime) -> MailMessage:
    """A bank alert that passes the DMARC check."""
    message = make_message(gmail_id, received_at)
    return dataclasses.replace(message, headers=(DMARC_PASS, *message.headers))


def _set_watermark(session: Session, value: datetime | None) -> None:
    # The dev DB may already hold a real watermark: pin it per test (rolled back afterwards).
    session.merge(SyncState(id=1, polled_until=value))
    session.flush()


def _watermark(session: Session) -> datetime | None:
    return session.scalar(select(SyncState.polled_until).where(SyncState.id == 1))


def _status(session: Session, gmail_id: str) -> MessageStatus | None:
    return session.scalar(select(Message.status).where(Message.gmail_id == gmail_id))


def _poll(
    session: Session, source: FakeMailSource, now: datetime = NOW
) -> tuple[int, int, int, int]:
    """(listed, recorded, unverified, failed)"""
    result = poll_once(session, source, PACKS, now=now, overlap=OVERLAP)
    return result.listed, result.recorded, result.unverified, result.failed


def test_first_poll_looks_back_one_day_and_sets_watermark(session: Session) -> None:
    _set_watermark(session, None)
    source = FakeMailSource()
    source.add(
        _mail("poll-recent", NOW - timedelta(hours=3)),
        _mail("poll-too-old", NOW - timedelta(days=2)),  # backfill's job, not the poller's
    )

    assert _poll(session, source) == (1, 1, 0, 0)

    expected_after = int((NOW - FIRST_POLL_LOOKBACK - OVERLAP).timestamp())
    assert f"after:{expected_after}" in source.queries[0]
    assert _status(session, "poll-recent") is MessageStatus.FETCHED
    assert _status(session, "poll-too-old") is None
    assert _watermark(session) == NOW


def test_overlap_catches_late_indexed_mail_without_refetching(session: Session) -> None:
    _set_watermark(session, NOW - timedelta(minutes=2))
    source = FakeMailSource()
    source.add(_mail("poll-a", NOW - timedelta(minutes=1)))
    _poll(session, source)

    # Received 3 min before the first poll but only searchable now (search index lag).
    source.add(_mail("poll-late", NOW - timedelta(minutes=3)))
    later = NOW + timedelta(minutes=2)

    assert _poll(session, source, now=later) == (2, 1, 0, 0)  # both listed, only one new
    assert source.fetched == ["poll-a", "poll-late"]  # poll-a not downloaded twice
    assert _watermark(session) == later


def test_mail_older_than_the_overlap_is_not_picked_up(session: Session) -> None:
    # The limit of the design: indexing lag longer than the overlap needs a backfill sweep.
    _set_watermark(session, NOW)
    source = FakeMailSource()
    source.add(_mail("poll-very-late", NOW - OVERLAP - timedelta(minutes=1)))

    assert _poll(session, source, now=NOW + timedelta(minutes=2)) == (0, 0, 0, 0)


def test_long_downtime_is_covered_by_the_watermark(session: Session) -> None:
    _set_watermark(session, NOW - timedelta(days=3))
    source = FakeMailSource()
    source.add(*(_mail(f"poll-day-{day}", NOW - timedelta(days=day)) for day in (1, 2)))

    assert _poll(session, source) == (2, 2, 0, 0)


def test_failed_dmarc_is_recorded_as_unverified(session: Session) -> None:
    _set_watermark(session, NOW - timedelta(minutes=2))
    source = FakeMailSource()
    source.add(make_message("poll-no-auth", NOW - timedelta(minutes=1)))  # no auth header

    assert _poll(session, source) == (1, 1, 1, 0)
    assert _status(session, "poll-no-auth") is MessageStatus.UNVERIFIED


class _FailingSource(FakeMailSource):
    """Network dies while downloading one particular message."""

    def __init__(self, fail_on: str) -> None:
        super().__init__()
        self.fail_on: str | None = fail_on

    def get_message(self, message_id: str) -> MailMessage:
        if message_id == self.fail_on:
            raise ConnectionError("network down")
        return super().get_message(message_id)


def test_network_error_mid_poll_keeps_watermark_and_next_poll_redoes_the_window(
    session: Session,
) -> None:
    start = NOW - timedelta(minutes=2)
    _set_watermark(session, start)
    session.commit()  # the watermark is the "already committed" state before this poll
    source = _FailingSource(fail_on="poll-2")
    source.add(
        _mail("poll-1", NOW - timedelta(seconds=90)),
        _mail("poll-2", NOW - timedelta(seconds=30)),
    )

    with pytest.raises(ConnectionError):
        _poll(session, source)
    session.rollback()  # what get_sessionmaker().begin() does when the block raises

    assert _watermark(session) == start
    assert _status(session, "poll-1") is None  # recorded in the failed poll, rolled back

    source.fail_on = None
    assert _poll(session, source, now=NOW + timedelta(minutes=2)) == (2, 2, 0, 0)


def test_poison_message_is_recorded_as_failed_and_does_not_block_the_poll(
    session: Session,
) -> None:
    _set_watermark(session, NOW - timedelta(minutes=2))
    source = FakeMailSource()
    source.add(_mail("poll-good-1", NOW - timedelta(seconds=90)))
    source.add_unparseable(_mail("poll-poison", NOW - timedelta(seconds=60)))
    source.add(_mail("poll-good-2", NOW - timedelta(seconds=30)))

    assert _poll(session, source) == (3, 2, 0, 1)

    row = session.scalars(select(Message).where(Message.gmail_id == "poll-poison")).one()
    assert row.status is MessageStatus.FAILED
    assert row.error == "parse: ValueError"  # class name only, no message text
    assert row.received_at == NOW - timedelta(seconds=60)
    assert _watermark(session) == NOW  # the poll moved on

    # Next poll: still listed (overlap), but already recorded, so never downloaded again.
    source.fetched.clear()
    assert _poll(session, source, now=NOW + timedelta(minutes=2)) == (3, 0, 0, 0)
    assert source.fetched == []


def test_auth_check_crash_is_recorded_as_failed(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_check(message: MailMessage) -> object:
        raise KeyError("unexpected header shape")

    monkeypatch.setattr(ingest, "check_dmarc", broken_check)
    _set_watermark(session, NOW - timedelta(minutes=2))
    source = FakeMailSource()
    source.add(_mail("poll-auth-bug", NOW - timedelta(minutes=1)))

    assert _poll(session, source) == (1, 0, 0, 1)
    assert _status(session, "poll-auth-bug") is MessageStatus.FAILED


def test_a_second_poller_waits_for_the_first(session: Session) -> None:
    # The row must exist as COMMITTED state, as it does after the first real poll, or the
    # other connection can't even see it. A committed row with polled_until NULL means
    # "never polled", so leaving it behind in the test DB is harmless.
    engine = session.get_bind().engine
    with engine.begin() as setup:
        setup.execute(insert(SyncState).values(id=1).on_conflict_do_nothing())

    _lock_state(session)  # first poller: holds the lock, has written nothing yet

    with Session(engine) as other:
        # lock_timeout turns "wait until the first poller commits" into an error.
        other.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(OperationalError, match="lock timeout"):
            _lock_state(other)
