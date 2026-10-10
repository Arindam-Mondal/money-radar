"""In-memory MailSource for tests: no network, no OAuth, deterministic."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime

from app.pipeline.gmail.source import HistoryDelta, HistoryExpiredError, MailMessage

# build_query emits epoch seconds. The fake applies them exactly: after:<ts> inclusive,
# before:<ts> exclusive. Real Gmail drifts by up to ~1 minute at the edges (measured), which
# is why backfill pads its windows and relies on idempotent inserts rather than exact edges.
_AFTER = re.compile(r"\bafter:(\d+)\b")
_BEFORE = re.compile(r"\bbefore:(\d+)\b")


def make_message(
    message_id: str,
    received_at: datetime,
    *,
    sender: str = "alerts@bank.example",
    subject: str = "Transaction alert",
    text: str | None = "Rs 100 debited",
    html: str | None = None,
) -> MailMessage:
    """A MailMessage with sensible fake defaults; override only what a test cares about."""
    return MailMessage(
        id=message_id,
        thread_id=message_id,
        received_at=received_at,
        headers=(("From", sender), ("Subject", subject)),
        text_body=text,
        html_body=html,
    )


class FakeMailSource:
    """Mimics the Gmail behaviour the pipeline relies on.

    - every add() bumps the mailbox history id (like Gmail)
    - changes_since() returns ids added after the cursor; expired cursors raise
    - iter_message_ids() honours after:/before: epoch filters, newest first (like Gmail)
    - records queries and fetches so tests can assert what the pipeline asked for
    """

    def __init__(self, start_history_id: int = 1000) -> None:
        self._messages: dict[str, MailMessage] = {}
        self._log: list[tuple[int, str]] = []  # (history id when added, message id)
        self._history_id = start_history_id
        self._oldest_valid = start_history_id
        self.queries: list[str] = []
        self.fetched: list[str] = []

    # --- test controls -------------------------------------------------------

    def add(self, *messages: MailMessage) -> None:
        for message in messages:
            self._history_id += 1
            self._messages[message.id] = message
            self._log.append((self._history_id, message.id))

    def expire_history(self) -> None:
        """Simulate Gmail dropping old history: any cursor before now gets a 404."""
        self._oldest_valid = self._history_id

    # --- MailSource ----------------------------------------------------------

    def current_history_id(self) -> str:
        return str(self._history_id)

    def changes_since(self, history_id: str) -> HistoryDelta:
        start = int(history_id)
        if start < self._oldest_valid:
            raise HistoryExpiredError(f"history id {history_id} expired")
        ids = [message_id for added_at, message_id in self._log if added_at > start]
        return HistoryDelta(message_ids=ids, history_id=str(self._history_id))

    def iter_message_ids(self, query: str) -> Iterator[str]:
        self.queries.append(query)
        after = _AFTER.search(query)
        before = _BEFORE.search(query)
        newest_first = sorted(self._messages.values(), key=lambda m: m.received_at, reverse=True)
        for message in newest_first:
            ts = message.received_at.timestamp()
            if after and ts < int(after.group(1)):
                continue
            if before and ts >= int(before.group(1)):
                continue
            yield message.id

    def get_message(self, message_id: str) -> MailMessage:
        self.fetched.append(message_id)
        try:
            return self._messages[message_id]
        except KeyError:
            raise KeyError(f"no message {message_id!r} in fake mailbox") from None
