"""record_message / unseen_gmail_ids against real Postgres (ON CONFLICT needs the real thing)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models import Message, MessageStatus
from app.pipeline import PIPELINE_VERSION
from app.pipeline.gmail.record import record_message, unseen_gmail_ids
from tests.fakes.mail_source import make_message

pytestmark = pytest.mark.integration

RECEIVED = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Message)) or 0


def test_record_inserts_a_fetched_row(session: Session) -> None:
    assert record_message(session, make_message("gm-1", RECEIVED)) is True

    row = session.scalars(select(Message).where(Message.gmail_id == "gm-1")).one()
    assert row.status is MessageStatus.FETCHED
    assert row.received_at == RECEIVED
    assert row.pipeline_version == PIPELINE_VERSION
    assert row.sender_domain is None  # filled by the auth check (1.5)


def test_recording_twice_keeps_one_row(session: Session) -> None:
    before = _count(session)
    message = make_message("gm-dup", RECEIVED)

    first = record_message(session, message)
    second = record_message(session, message)

    assert (first, second) == (True, False)
    assert _count(session) == before + 1


def test_status_is_stored_as_the_lowercase_value(session: Session) -> None:
    record_message(session, make_message("gm-enum", RECEIVED))

    raw = session.execute(
        text("SELECT status::text FROM messages WHERE gmail_id = 'gm-enum'")
    ).scalar_one()
    assert raw == "fetched"


def test_database_rejects_unknown_status(session: Session) -> None:
    with pytest.raises(DBAPIError, match="invalid input value for enum message_status"):
        session.execute(
            text(
                "INSERT INTO messages (gmail_id, received_at, status, pipeline_version) "
                "VALUES ('gm-bad', now(), 'banana', 1)"
            )
        )


def test_unseen_gmail_ids_filters_known_keeps_order_and_dedupes(session: Session) -> None:
    record_message(session, make_message("known-1", RECEIVED))
    record_message(session, make_message("known-2", RECEIVED))

    unseen = unseen_gmail_ids(session, ["new-b", "known-1", "new-a", "new-b", "known-2"])

    assert unseen == ["new-b", "new-a"]


def test_unseen_gmail_ids_empty_input(session: Session) -> None:
    assert unseen_gmail_ids(session, []) == []
