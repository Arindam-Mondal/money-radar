"""GmailMailSource: the MailSource port implemented on the Gmail REST API (read-only)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from http import HTTPStatus
from typing import TYPE_CHECKING, Final, Protocol, TypeVar

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.pipeline.gmail.auth import load_credentials
from app.pipeline.gmail.parse import parse_message, received_at_of
from app.pipeline.gmail.source import (
    HistoryDelta,
    HistoryExpiredError,
    MailMessage,
    MessageParseError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from googleapiclient._apis.gmail.v1.resources import (
        GmailResource,
        ListHistoryResponseHttpRequest,
        ListMessagesResponseHttpRequest,
    )

_USER: Final = "me"  # the account the token belongs to
_PAGE_SIZE: Final = 500  # Gmail's maximum for messages.list and history.list
_RETRIES: Final = 3  # execute() retries 5xx/429 with exponential backoff (~1-8 s in total)
# Gmail's quota is per user per MINUTE, so a few seconds of backoff isn't enough: once
# execute() has given up, wait for the window to pass. ~2.75 min in total, then give up.
RATE_LIMIT_WAITS: Final = (15.0, 30.0, 60.0, 60.0)
_RATE_LIMIT_REASONS: Final = frozenset({"ratelimitexceeded", "userratelimitexceeded"})

logger = logging.getLogger("money_radar.pipeline")

_T_co = TypeVar("_T_co", covariant=True)


class _Request(Protocol[_T_co]):
    """Any googleapiclient request: they differ only in what execute() returns."""

    def execute(self, *, num_retries: int = ...) -> _T_co: ...


class GmailMailSource:
    """Not thread-safe (httplib2 underneath): use one instance per thread."""

    def __init__(
        self, service: GmailResource, *, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self._service = service
        self._sleep = sleep  # injectable so tests don't actually wait

    @classmethod
    def from_token_file(cls, token_path: Path) -> GmailMailSource:
        creds = load_credentials(token_path)
        return cls(build("gmail", "v1", credentials=creds, cache_discovery=False))

    def current_history_id(self) -> str:
        profile = self._execute(self._service.users().getProfile(userId=_USER))
        history_id = profile.get("historyId")
        if not history_id:
            raise RuntimeError("Gmail profile response has no historyId")
        return history_id

    def changes_since(self, history_id: str) -> HistoryDelta:
        history = self._service.users().history()
        request: ListHistoryResponseHttpRequest | None = history.list(
            userId=_USER,
            startHistoryId=history_id,
            historyTypes=["messageAdded"],
            maxResults=_PAGE_SIZE,
        )
        message_ids: list[str] = []
        latest = history_id
        while request is not None:
            try:
                response = self._execute(request)
            except HttpError as e:
                if e.resp.status == HTTPStatus.NOT_FOUND:
                    raise HistoryExpiredError(f"history id {history_id} expired") from e
                raise
            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    message_id = added.get("message", {}).get("id")
                    if message_id:
                        message_ids.append(message_id)
            latest = response.get("historyId", latest)
            request = history.list_next(request, response)
        return HistoryDelta(message_ids=message_ids, history_id=latest)

    def iter_message_ids(self, query: str) -> Iterator[str]:
        messages = self._service.users().messages()
        request: ListMessagesResponseHttpRequest | None = messages.list(
            userId=_USER, q=query, maxResults=_PAGE_SIZE
        )
        while request is not None:
            response = self._execute(request)
            for ref in response.get("messages", []):
                message_id = ref.get("id")
                if message_id:
                    yield message_id
            request = messages.list_next(request, response)

    def get_message(self, message_id: str) -> MailMessage:
        raw = self._execute(
            self._service.users().messages().get(userId=_USER, id=message_id, format="full")
        )
        try:
            return parse_message(raw)
        except Exception as e:  # parsing is pure: any error is about this message's data
            raise MessageParseError(message_id, received_at_of(raw)) from e

    def _execute(self, request: _Request[_T_co]) -> _T_co:
        """execute() with its own short retries, plus long waits when Gmail rate-limits us."""
        for wait in RATE_LIMIT_WAITS:
            try:
                return request.execute(num_retries=_RETRIES)
            except HttpError as e:
                if not _is_rate_limit(e):
                    raise
                logger.warning("Gmail rate limit reached; waiting %.0f s", wait)
                self._sleep(wait)
        return request.execute(num_retries=_RETRIES)  # last try: a rate limit now propagates


def _is_rate_limit(error: HttpError) -> bool:
    """429, or a 403 whose reason is a rate limit (a 403 can also mean "no permission")."""
    if error.resp.status == HTTPStatus.TOO_MANY_REQUESTS:
        return True
    if error.resp.status != HTTPStatus.FORBIDDEN:
        return False
    details: list[object] = error.error_details if isinstance(error.error_details, list) else []
    return any(
        isinstance(detail, dict)
        and str(detail.get("reason", "")).replace("_", "").lower() in _RATE_LIMIT_REASONS
        for detail in details
    )
