"""MailSource port: what the pipeline needs from a mailbox, independent of Gmail's API."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class HistoryExpiredError(Exception):
    """The start history id is too old for Gmail; caller must fall back to a full sync."""


@dataclass(frozen=True, slots=True)
class MailMessage:
    id: str  # Gmail message id (hex string) -> messages.gmail_id
    thread_id: str
    received_at: datetime  # tz-aware UTC, from Gmail internalDate (not the Date header)
    headers: tuple[tuple[str, str], ...]  # (name, value) in order; names can repeat
    text_body: str | None  # decoded text/plain part, if any
    html_body: str | None  # decoded text/html part, if any

    def header(self, name: str) -> str | None:
        """First value of header `name` (case-insensitive), or None if absent."""
        wanted = name.lower()
        return next((value for key, value in self.headers if key.lower() == wanted), None)

    def header_all(self, name: str) -> list[str]:
        """Every value of header `name` (case-insensitive), in message order."""
        wanted = name.lower()
        return [value for key, value in self.headers if key.lower() == wanted]


@dataclass(frozen=True, slots=True)
class HistoryDelta:
    message_ids: list[str]  # messages added since the start id (may repeat; caller dedupes via DB)
    history_id: str  # new cursor to save in sync_state


class MailSource(Protocol):
    def current_history_id(self) -> str: ...
    def changes_since(self, history_id: str) -> HistoryDelta: ...  # raises HistoryExpiredError
    def iter_message_ids(self, query: str) -> Iterator[str]: ...
    def get_message(self, message_id: str) -> MailMessage: ...
