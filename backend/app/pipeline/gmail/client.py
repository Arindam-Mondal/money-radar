"""GmailMailSource: the MailSource port implemented on the Gmail REST API (read-only)."""

from __future__ import annotations

from collections.abc import Iterator
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.pipeline.gmail.auth import load_credentials
from app.pipeline.gmail.parse import parse_message
from app.pipeline.gmail.source import HistoryDelta, HistoryExpiredError, MailMessage

if TYPE_CHECKING:
    from pathlib import Path

    from googleapiclient._apis.gmail.v1.resources import (
        GmailResource,
        ListHistoryResponseHttpRequest,
        ListMessagesResponseHttpRequest,
    )

_USER: Final = "me"  # the account the token belongs to
_PAGE_SIZE: Final = 500  # Gmail's maximum for messages.list and history.list
_RETRIES: Final = 3  # execute() retries 5xx/429 with exponential backoff


class GmailMailSource:
    """Not thread-safe (httplib2 underneath): use one instance per thread."""

    def __init__(self, service: GmailResource) -> None:
        self._service = service

    @classmethod
    def from_token_file(cls, token_path: Path) -> GmailMailSource:
        creds = load_credentials(token_path)
        return cls(build("gmail", "v1", credentials=creds, cache_discovery=False))

    def current_history_id(self) -> str:
        profile = self._service.users().getProfile(userId=_USER).execute(num_retries=_RETRIES)
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
                response = request.execute(num_retries=_RETRIES)
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
            response = request.execute(num_retries=_RETRIES)
            for ref in response.get("messages", []):
                message_id = ref.get("id")
                if message_id:
                    yield message_id
            request = messages.list_next(request, response)

    def get_message(self, message_id: str) -> MailMessage:
        raw = (
            self._service.users()
            .messages()
            .get(userId=_USER, id=message_id, format="full")
            .execute(num_retries=_RETRIES)
        )
        return parse_message(raw)
