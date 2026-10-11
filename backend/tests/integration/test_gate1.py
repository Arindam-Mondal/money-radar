"""Gate 1 (end of P1): backfilling a month twice gives identical results, zero duplicates.

"Identical" is checked at two strengths:
- _rows(): every column of every row, plus `id` and `xmin` (the id of the transaction that
  wrote the row version). If a run rewrote a row, even with the same values, xmin changes.
  Equal _rows() before and after a run means that run wrote nothing at all.
- _content(): what each message ended up as (status, auth verdict, dates, error), keyed by
  gmail_id. Equal _content() means two different histories (crash + resume, chunks in any
  order, poll + backfill + re-sweep) reached the same result as one clean run.

The mailbox is one realistic month: two alerts a day, IST midnight edges, mail just outside
the month (inside and outside the search padding), DMARC failures and a poison message.
"""

import dataclasses
import random
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Connection, delete, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app import jobqueue
from app.config import Settings
from app.db.models import Job, JobKind, JobStatus, Message, MessageStatus, SyncRange, SyncState
from app.pipeline.gmail import backfill as backfill_module
from app.pipeline.gmail.backfill import BackfillResult, backfill
from app.pipeline.gmail.poller import poll_once
from app.pipeline.gmail.ranges import DateRange, merge
from app.pipeline.gmail.source import MailMessage
from app.regions import load_region_packs
from app.worker.backfills import run_backfills
from tests.fakes.mail_source import FakeMailSource, make_message

pytestmark = pytest.mark.integration

IST = ZoneInfo("Asia/Kolkata")
PACKS = load_region_packs(["india"])
SEPTEMBER = DateRange(date(2026, 9, 1), date(2026, 10, 1))
NOW = datetime(2026, 10, 15, 6, 30, tzinfo=UTC)  # long after September: every day is complete
DMARC_PASS = ("Authentication-Results", "mx.google.com; dmarc=pass header.from=bank.example")

FETCHED, UNVERIFIED, FAILED = MessageStatus.FETCHED, MessageStatus.UNVERIFIED, MessageStatus.FAILED


# --- the mailbox and what a correct backfill must make of it --------------------------------


def _ist(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=IST)


def _alert(gmail_id: str, received_at: datetime) -> MailMessage:
    message = make_message(gmail_id, received_at)
    return dataclasses.replace(message, headers=(DMARC_PASS, *message.headers))


def _mailbox(
    source: FakeMailSource | None = None, *, until: datetime | None = None
) -> tuple[FakeMailSource, dict[str, MessageStatus]]:
    """Fill `source` (default: a new FakeMailSource) with the month; also return the oracle:
    gmail_id -> status, for every message that must end up in `messages` (and only those).

    `until`: the mailbox as it was at that moment (later mail hasn't arrived yet).
    """
    source = FakeMailSource() if source is None else source
    expected: dict[str, MessageStatus] = {}

    def add(gmail_id: str, at: datetime, status: MessageStatus | None) -> None:
        if status is not None:
            expected[gmail_id] = status
        if until is not None and at > until:
            return
        if status is FAILED:
            source.add_unparseable(make_message(gmail_id, at))
        elif status is UNVERIFIED:
            source.add(make_message(gmail_id, at))  # no Authentication-Results: fails DMARC
        else:
            source.add(_alert(gmail_id, at))

    for day in range(1, 31):
        add(f"g1-sep-{day:02d}-a", _ist(9, day, 9 + day % 7, 15), FETCHED)
        add(f"g1-sep-{day:02d}-b", _ist(9, day, 18, 30), FETCHED)
    add("g1-edge-first", _ist(9, 1, 0, 5), FETCHED)  # Aug 31 in UTC: September in IST
    add("g1-edge-last", _ist(9, 30, 23, 58), FETCHED)
    # Inside the 10-minute search padding: recorded (once), though not September mail.
    add("g1-pad-before", _ist(8, 31, 23, 55), FETCHED)
    add("g1-pad-after", _ist(10, 1, 0, 5), FETCHED)
    # Well outside the month: never recorded.
    add("g1-out-aug", _ist(8, 31, 12), None)
    add("g1-out-oct", _ist(10, 1, 12), None)
    add("g1-nodmarc-1", _ist(9, 10, 11), UNVERIFIED)
    add("g1-nodmarc-2", _ist(9, 20, 11), UNVERIFIED)
    add("g1-poison", _ist(9, 15, 13), FAILED)
    return source, expected


# --- snapshots --------------------------------------------------------------------------------

_SNAPSHOT = text(
    "select id, gmail_id, status::text, auth_result, sender_domain, received_at,"
    " pipeline_version, error, xmin::text"
    " from messages where gmail_id like 'g1-%' order by gmail_id"
)


def _rows(session: Session) -> list[tuple[Any, ...]]:
    """Exact physical state: same ids, same values, same row versions (xmin)."""
    return [tuple(row) for row in session.execute(_SNAPSHOT)]


def _content(session: Session) -> dict[str, tuple[Any, ...]]:
    """What each message ended up as, independent of insert order and row versions."""
    return {row[1]: row[2:8] for row in _rows(session)}


def _statuses(session: Session) -> dict[str, MessageStatus]:
    return {gmail_id: MessageStatus(values[0]) for gmail_id, values in _content(session).items()}


def _coverage(session: Session) -> list[DateRange]:
    return merge(DateRange(r.start_date, r.end_date) for r in session.scalars(select(SyncRange)))


def _wipe(session: Session) -> None:
    session.execute(delete(Message).where(Message.gmail_id.like("g1-%")))
    session.execute(delete(SyncRange))
    session.commit()


# --- running it -------------------------------------------------------------------------------


class _DiesAfter(FakeMailSource):
    """The network goes away after `limit` downloads (None: never)."""

    limit: int | None = None

    def get_message(self, message_id: str) -> MailMessage:
        if self.limit is not None and len(self.fetched) >= self.limit:
            raise ConnectionError("network down")
        return super().get_message(message_id)


def _dying_mailbox(limit: int) -> _DiesAfter:
    source = _DiesAfter()
    source.limit = limit
    _mailbox(source)  # same mailbox, flaky network
    return source


def _backfill(
    session: Session,
    source: FakeMailSource,
    requested: DateRange = SEPTEMBER,
    *,
    use_coverage: bool = True,
) -> BackfillResult:
    return backfill(session, source, PACKS, requested, zone=IST, now=NOW, use_coverage=use_coverage)


@pytest.fixture(autouse=True)
def _setup(session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Several batches per run, so a crash can land between commits. Clean slate in the
    # (rolled back) test transaction: the dev DB may hold real coverage and jobs.
    monkeypatch.setattr(backfill_module, "BATCH_SIZE", 7)
    session.execute(delete(SyncRange))
    session.execute(delete(Job))
    session.flush()
    yield


@pytest.fixture
def clean_result(session: Session) -> dict[str, tuple[Any, ...]]:
    """_content() after one clean backfill of September; the database is wiped again after."""
    source, _ = _mailbox()
    _backfill(session, source)
    content = _content(session)
    _wipe(session)
    return content


# --- the gate ---------------------------------------------------------------------------------


def test_one_backfill_records_exactly_the_expected_messages(session: Session) -> None:
    source, expected = _mailbox()

    result = _backfill(session, source)

    assert _statuses(session) == expected  # nothing missing, nothing extra, right status each
    unverified = sum(1 for s in expected.values() if s is UNVERIFIED)
    failed = sum(1 for s in expected.values() if s is FAILED)
    assert (result.counts.recorded, result.counts.unverified, result.counts.failed) == (
        len(expected) - failed,
        unverified,
        failed,
    )
    assert _coverage(session) == [SEPTEMBER]


def test_backfilling_the_month_twice_changes_nothing(session: Session) -> None:
    source, _ = _mailbox()
    _backfill(session, source)
    before = _rows(session)
    source.queries.clear()
    source.fetched.clear()

    again = _backfill(session, source)

    assert _rows(session) == before  # same ids, values and row versions: zero writes
    assert (again.gaps, again.listed, source.queries, source.fetched) == ([], 0, [], [])


def test_with_coverage_forgotten_the_second_run_still_changes_nothing(session: Session) -> None:
    # Coverage is only an optimisation. Idempotency must come from the messages table itself.
    source, _ = _mailbox()
    first = _backfill(session, source)
    before = _rows(session)
    session.execute(delete(SyncRange))
    session.commit()
    source.fetched.clear()

    again = _backfill(session, source)

    assert again.listed == first.listed  # Gmail listed everything again...
    assert source.fetched == []  # ...but nothing was downloaded
    assert (again.counts.recorded, again.counts.unverified, again.counts.failed) == (0, 0, 0)
    assert _rows(session) == before
    assert _coverage(session) == [SEPTEMBER]


def test_the_snapshot_notices_a_rewrite_with_identical_values(session: Session) -> None:
    # Guards the guard: if _rows() couldn't see a no-op UPDATE, the tests above would prove
    # less than they claim.
    source, _ = _mailbox()
    _backfill(session, source)
    before = _rows(session)

    session.execute(
        update(Message).where(Message.gmail_id == "g1-sep-01-a").values(status=Message.status)
    )
    session.commit()

    assert _content(session) == {row[1]: row[2:8] for row in before}  # same values...
    assert _rows(session) != before  # ...but a new row version


@pytest.mark.parametrize("crash_after", [0, 1, 7, 30, 66])
def test_crash_anywhere_then_resume_equals_one_clean_run(
    session: Session, clean_result: dict[str, tuple[Any, ...]], crash_after: int
) -> None:
    source = _dying_mailbox(crash_after)

    with pytest.raises(ConnectionError):
        _backfill(session, source)
    session.rollback()  # the batch in flight; earlier batches are committed
    assert _coverage(session) == []  # an unfinished month is never marked covered

    source.limit = None
    _backfill(session, source)

    assert _content(session) == clean_result
    assert _coverage(session) == [SEPTEMBER]


@pytest.mark.parametrize("seed", range(5))
def test_any_split_and_order_of_the_month_equals_one_clean_run(
    session: Session, clean_result: dict[str, tuple[Any, ...]], seed: int
) -> None:
    rng = random.Random(seed)  # noqa: S311 - reproducible test shuffles, not secrets
    cuts = sorted(rng.sample(range(1, SEPTEMBER.days), k=4))
    edges = [SEPTEMBER.start + timedelta(days=d) for d in (0, *cuts, SEPTEMBER.days)]
    chunks = []
    for start, end in pairwise(edges):
        # Stretch some chunks into their neighbours: requests overlap like real ones do.
        stretched_end = min(end + timedelta(days=rng.randint(0, 3)), SEPTEMBER.end)
        chunks.append(DateRange(start, stretched_end))
    rng.shuffle(chunks)
    source, _ = _mailbox()

    listed = sum(_backfill(session, source, chunk).listed for chunk in chunks)

    assert _content(session) == clean_result
    assert _coverage(session) == [SEPTEMBER]
    assert listed >= len(clean_result)  # chunk edges re-list padded neighbours: deduped
    assert max(Counter(source.fetched).values()) == 1  # and nothing downloaded twice


def test_poll_backfill_and_resweep_together_equal_one_clean_run(
    session: Session, clean_result: dict[str, tuple[Any, ...]]
) -> None:
    # The worker started on Sep 30 evening: its first poll looks back one day, and can only
    # see mail that had arrived by then.
    poll_at = _ist(9, 30, 21)
    then, _ = _mailbox(until=poll_at)
    session.merge(SyncState(id=1, polled_until=None))
    poll = poll_once(session, then, PACKS, now=poll_at, overlap=timedelta(minutes=10))
    session.commit()
    # Weeks later the user backfilled the month, and the nightly re-sweep covered Sep 29-30.
    source, _ = _mailbox()
    _backfill(session, source)
    resweep = DateRange(date(2026, 9, 29), date(2026, 10, 1))
    _backfill(session, source, resweep, use_coverage=False)

    assert poll.recorded > 0  # the three paths really did overlap
    assert _content(session) == clean_result
    downloads = Counter(then.fetched) + Counter(source.fetched)
    assert max(downloads.values()) == 1  # each message downloaded once, by whoever came first


class _ShiftingPages(FakeMailSource):
    """Lists every id twice, like Gmail search pages shifting while new mail arrives: the
    repeat lands in the same batch for most ids and in the next batch for some."""

    def iter_message_ids(self, query: str) -> Iterator[str]:
        for gmail_id in super().iter_message_ids(query):
            yield gmail_id
            yield gmail_id


def test_ids_listed_twice_are_recorded_and_downloaded_once(
    session: Session, clean_result: dict[str, tuple[Any, ...]]
) -> None:
    source = _ShiftingPages()
    _mailbox(source)

    result = _backfill(session, source)

    assert result.listed == 2 * len(clean_result)
    assert _content(session) == clean_result
    assert max(Counter(source.fetched).values()) == 1


# --- the same through the job queue (what POST /backfill does) --------------------------------


@pytest.fixture
def sessions(session: Session) -> sessionmaker[Session]:
    connection = session.get_bind()
    assert isinstance(connection, Connection)
    return sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


def test_two_queued_backfills_of_the_month_change_nothing_the_second_time(
    session: Session, sessions: sessionmaker[Session]
) -> None:
    source, expected = _mailbox()
    params = {"start": "2026-09-01", "end": "2026-10-01"}

    def queue_and_run() -> Job:
        job = jobqueue.enqueue(session, JobKind.BACKFILL, params, run_after=NOW)
        session.commit()
        run_backfills(
            Settings(), PACKS, sessions=sessions, source_factory=lambda _: source, clock=lambda: NOW
        )
        session.expire_all()
        return session.scalars(select(Job).where(Job.id == job.id)).one()

    first = queue_and_run()
    before = _rows(session)
    second = queue_and_run()

    assert (first.status, second.status) == (JobStatus.DONE, JobStatus.DONE)
    assert first.progress["recorded"] + first.progress["failed"] == len(expected)
    assert (second.progress["gaps_total"], second.progress["recorded"]) == (0, 0)
    assert _rows(session) == before
