"""The fake must behave like Gmail where the pipeline depends on it."""

from datetime import UTC, datetime

import pytest

from app.pipeline.gmail.source import HistoryExpiredError, MailSource
from tests.fakes.mail_source import FakeMailSource, make_message

JAN_1 = datetime(2026, 1, 1, tzinfo=UTC)
JAN_2 = datetime(2026, 1, 2, tzinfo=UTC)
JAN_3 = datetime(2026, 1, 3, tzinfo=UTC)


def test_fake_satisfies_the_mailsource_protocol() -> None:
    source: MailSource = FakeMailSource()  # mypy checks the structural match here

    assert source.current_history_id() == "1000"


def test_changes_since_returns_only_messages_added_after_cursor() -> None:
    fake = FakeMailSource()
    fake.add(make_message("a", JAN_1))
    cursor = fake.current_history_id()
    fake.add(make_message("b", JAN_2), make_message("c", JAN_3))

    delta = fake.changes_since(cursor)

    assert delta.message_ids == ["b", "c"]
    assert delta.history_id == fake.current_history_id()
    assert fake.changes_since(delta.history_id).message_ids == []


def test_expired_cursor_raises() -> None:
    fake = FakeMailSource()
    cursor = fake.current_history_id()
    fake.add(make_message("a", JAN_1))
    fake.expire_history()

    with pytest.raises(HistoryExpiredError):
        fake.changes_since(cursor)


def test_iter_message_ids_filters_half_open_window_newest_first() -> None:
    fake = FakeMailSource()
    fake.add(make_message("d1", JAN_1), make_message("d2", JAN_2), make_message("d3", JAN_3))
    query = f"from:bank.example after:{int(JAN_1.timestamp())} before:{int(JAN_3.timestamp())}"

    ids = list(fake.iter_message_ids(query))

    assert ids == ["d2", "d1"]  # JAN_3 excluded: before: is exclusive
    assert fake.queries == [query]


def test_get_message_records_fetches_and_rejects_unknown_ids() -> None:
    fake = FakeMailSource()
    fake.add(make_message("a", JAN_1))

    assert fake.get_message("a").id == "a"
    assert fake.fetched == ["a"]
    with pytest.raises(KeyError):
        fake.get_message("missing")
