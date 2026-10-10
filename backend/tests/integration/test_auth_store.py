"""apply_auth_verdict against real Postgres."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import Message, MessageStatus
from app.pipeline.auth.dmarc import AuthVerdict
from app.pipeline.auth.store import apply_auth_verdict
from app.pipeline.gmail.record import record_message
from tests.fakes.mail_source import make_message

pytestmark = pytest.mark.integration

PASSED = AuthVerdict(passed=True, sender_domain="bank.example", reason="dmarc=pass")
FAILED = AuthVerdict(passed=False, sender_domain="bank.example", reason="dmarc=fail")


def _row(session: Session, gmail_id: str) -> Message:
    session.expire_all()  # re-read from the database, not the identity map
    return session.scalars(select(Message).where(Message.gmail_id == gmail_id)).one()


def _recorded(session: Session, gmail_id: str) -> str:
    record_message(session, make_message(gmail_id, datetime(2026, 1, 1, tzinfo=UTC)))
    return gmail_id


def test_pass_keeps_fetched_and_saves_domain_and_result(session: Session) -> None:
    gmail_id = _recorded(session, "auth-pass")

    assert apply_auth_verdict(session, gmail_id, PASSED) is True

    row = _row(session, gmail_id)
    assert (row.status, row.sender_domain, row.auth_result) == (
        MessageStatus.FETCHED,
        "bank.example",
        "dmarc=pass",
    )


def test_fail_moves_the_message_to_unverified(session: Session) -> None:
    gmail_id = _recorded(session, "auth-fail")

    apply_auth_verdict(session, gmail_id, FAILED)

    row = _row(session, gmail_id)
    assert (row.status, row.auth_result) == (MessageStatus.UNVERIFIED, "dmarc=fail")


def test_message_past_fetched_is_left_alone(session: Session) -> None:
    gmail_id = _recorded(session, "auth-later")
    session.execute(
        update(Message).where(Message.gmail_id == gmail_id).values(status=MessageStatus.EXTRACTED)
    )

    assert apply_auth_verdict(session, gmail_id, FAILED) is False
    assert _row(session, gmail_id).status is MessageStatus.EXTRACTED


def test_unknown_message_updates_nothing(session: Session) -> None:
    assert apply_auth_verdict(session, "never-recorded", PASSED) is False
