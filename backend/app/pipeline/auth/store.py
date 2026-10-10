"""Save the DMARC verdict on the message row; failures are quarantined as `unverified`."""

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.models import Message, MessageStatus
from app.pipeline.auth.dmarc import AuthVerdict


def apply_auth_verdict(session: Session, gmail_id: str, verdict: AuthVerdict) -> bool:
    """Record sender_domain + auth_result; a failed check moves the row to `unverified`.

    Only touches rows still in `fetched`, so re-running it can't drag a message that has
    moved on (extracted, needs_review, ...) back. Returns True if a row was updated.
    Does not commit: the caller owns the transaction.
    """
    values: dict[str, object] = {
        "sender_domain": verdict.sender_domain,
        "auth_result": verdict.reason,
    }
    if not verdict.passed:
        values["status"] = MessageStatus.UNVERIFIED
    statement = (
        update(Message)
        .where(Message.gmail_id == gmail_id, Message.status == MessageStatus.FETCHED)
        .values(values)
        .returning(Message.id)
    )
    return session.execute(statement).scalar_one_or_none() is not None
