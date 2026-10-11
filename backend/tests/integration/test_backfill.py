"""backfill against real Postgres + FakeMailSource: gaps, coverage, crash, today, time zone."""

import dataclasses
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Message, SyncRange
from app.pipeline.gmail import backfill as backfill_module
from app.pipeline.gmail.backfill import EDGE_PAD, BackfillResult, backfill
from app.pipeline.gmail.ranges import DateRange
from app.pipeline.gmail.source import MailMessage
from app.regions import load_region_packs
from tests.fakes.mail_source import FakeMailSource, make_message

pytestmark = pytest.mark.integration

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)  # "today" is 2026-03-15 in IST
PACKS = load_region_packs(["india"])
DMARC_PASS = ("Authentication-Results", "mx.google.com; dmarc=pass header.from=bank.example")
JAN = DateRange(date(2026, 1, 1), date(2026, 2, 1))


@pytest.fixture(autouse=True)
def _no_coverage(session: Session) -> Iterator[None]:
    # The dev DB may hold real coverage from running the CLI: start each test without it.
    session.execute(delete(SyncRange))
    session.flush()
    yield


def _mail(gmail_id: str, received_at: datetime) -> MailMessage:
    message = make_message(gmail_id, received_at)
    return dataclasses.replace(message, headers=(DMARC_PASS, *message.headers))


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=IST)


def _run(session: Session, source: FakeMailSource, requested: DateRange) -> BackfillResult:
    return backfill(session, source, PACKS, requested, zone=IST, now=NOW)


def _coverage(session: Session) -> list[tuple[date, date]]:
    rows = session.scalars(select(SyncRange).order_by(SyncRange.start_date))
    return [(r.start_date, r.end_date) for r in rows]


def _recorded(session: Session, prefix: str) -> set[str]:
    rows = session.scalars(select(Message.gmail_id).where(Message.gmail_id.like(f"{prefix}%")))
    return set(rows)


def test_first_backfill_ingests_the_range_and_records_coverage(session: Session) -> None:
    source = FakeMailSource()
    source.add(
        _mail("bf-jan-05", _at(date(2026, 1, 5))),
        _mail("bf-jan-31", _at(date(2026, 1, 31))),
        _mail("bf-feb-03", _at(date(2026, 2, 3))),  # outside the request
    )

    result = _run(session, source, JAN)

    assert result.gaps == [JAN]
    assert result.counts.recorded == 2
    assert _recorded(session, "bf-") == {"bf-jan-05", "bf-jan-31"}
    assert _coverage(session) == [(date(2026, 1, 1), date(2026, 2, 1))]


def test_second_run_fetches_nothing(session: Session) -> None:
    source = FakeMailSource()
    source.add(_mail("bf-again", _at(date(2026, 1, 10))))
    _run(session, source, JAN)
    source.queries.clear()
    source.fetched.clear()

    result = _run(session, source, JAN)

    assert (result.gaps, result.listed, result.counts.recorded) == ([], 0, 0)
    assert source.queries == []  # not even a search: the coverage table answered it
    assert source.fetched == []


def test_only_the_uncovered_part_is_searched(session: Session) -> None:
    session.add(SyncRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 10)))
    session.flush()
    source = FakeMailSource()

    result = _run(session, source, DateRange(date(2026, 1, 1), date(2026, 1, 20)))

    assert result.gaps == [DateRange(date(2026, 1, 10), date(2026, 1, 20))]
    after = int((datetime(2026, 1, 10, tzinfo=IST) - EDGE_PAD).timestamp())
    before = int((datetime(2026, 1, 20, tzinfo=IST) + EDGE_PAD).timestamp())
    assert len(source.queries) == 1  # one search, for the one gap
    assert f"after:{after}" in source.queries[0]
    assert f"before:{before}" in source.queries[0]


def test_day_boundaries_are_in_the_configured_zone(session: Session) -> None:
    # 00:30 IST on Jan 1 is still Dec 31 in UTC. It belongs to January for an Indian user.
    source = FakeMailSource()
    source.add(_mail("bf-ist-midnight", datetime(2026, 1, 1, 0, 30, tzinfo=IST)))

    _run(session, source, DateRange(date(2026, 1, 1), date(2026, 1, 2)))

    assert _recorded(session, "bf-ist") == {"bf-ist-midnight"}


def test_today_and_later_are_not_marked_covered(session: Session) -> None:
    today = NOW.astimezone(IST).date()
    source = FakeMailSource()

    result = _run(session, source, DateRange(today - timedelta(days=3), today + timedelta(days=5)))

    assert result.requested == DateRange(today - timedelta(days=3), today)
    assert _coverage(session) == [(today - timedelta(days=3), today)]


def test_a_request_starting_today_does_nothing(session: Session) -> None:
    today = NOW.astimezone(IST).date()

    result = _run(session, FakeMailSource(), DateRange(today, today + timedelta(days=1)))

    assert result.requested is None
    assert _coverage(session) == []


def test_resweep_searches_covered_days_and_adds_no_coverage(session: Session) -> None:
    # Jan 10 was covered, then Gmail indexed one more Jan 10 mail late: only a re-sweep sees it.
    session.add(SyncRange(start_date=date(2026, 1, 10), end_date=date(2026, 1, 11)))
    session.flush()
    source = FakeMailSource()
    source.add(_mail("bf-late", _at(date(2026, 1, 10))), _mail("bf-new", _at(date(2026, 1, 11))))
    requested = DateRange(date(2026, 1, 10), date(2026, 1, 12))

    result = backfill(session, source, PACKS, requested, zone=IST, now=NOW, use_coverage=False)

    assert result.gaps == [requested]  # the whole range, covered part included
    assert _recorded(session, "bf-") == {"bf-late", "bf-new"}
    assert _coverage(session) == [(date(2026, 1, 10), date(2026, 1, 11))]  # unchanged


class _DiesAfter(FakeMailSource):
    """The network goes away after `limit` messages have been downloaded."""

    def __init__(self, limit: int | None) -> None:
        super().__init__()
        self.limit = limit

    def get_message(self, message_id: str) -> MailMessage:
        if self.limit is not None and len(self.fetched) >= self.limit:
            raise ConnectionError("network down")
        return super().get_message(message_id)


def test_crash_mid_gap_keeps_finished_batches_but_not_the_coverage(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backfill_module, "BATCH_SIZE", 2)
    source = _DiesAfter(limit=3)
    source.add(*(_mail(f"bf-crash-{day:02d}", _at(date(2026, 1, day))) for day in range(1, 6)))

    with pytest.raises(ConnectionError):
        _run(session, source, JAN)
    session.rollback()  # the unfinished batch; earlier batches were already committed

    assert len(_recorded(session, "bf-crash-")) == 2  # first batch of 2 survived
    assert _coverage(session) == []  # the gap was not finished, so it is not covered

    source.limit = None
    result = _run(session, source, JAN)  # resume: same gap, finished work skipped

    assert result.counts.recorded == 3
    assert len(_recorded(session, "bf-crash-")) == 5
    assert _coverage(session) == [(date(2026, 1, 1), date(2026, 2, 1))]


def test_database_rejects_an_empty_range(session: Session) -> None:
    session.add(SyncRange(start_date=date(2026, 1, 5), end_date=date(2026, 1, 5)))
    with pytest.raises(IntegrityError, match="ck_sync_ranges_nonempty"):
        session.flush()
