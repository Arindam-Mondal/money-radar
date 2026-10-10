"""GmailMailSource against a mocked Gmail service: paging, 404 translation, delegation."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.pipeline.gmail.client import GmailMailSource
from app.pipeline.gmail.source import HistoryExpiredError


def _http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": str(status)}), b"{}")


def _paged(resource: MagicMock, pages: list[dict[str, Any]]) -> None:
    """Wire resource.list()/list_next() to return one request per page, then None."""
    requests = [MagicMock(name=f"page{i}") for i in range(len(pages))]
    for request, page in zip(requests, pages, strict=True):
        request.execute.return_value = page
    resource.list.return_value = requests[0]
    resource.list_next.side_effect = [*requests[1:], None]


def test_current_history_id() -> None:
    service = MagicMock()
    service.users().getProfile().execute.return_value = {"historyId": "9001"}

    assert GmailMailSource(service).current_history_id() == "9001"


def test_changes_since_collects_ids_across_pages_and_returns_latest_cursor() -> None:
    service = MagicMock()
    history = service.users().history()
    _paged(
        history,
        [
            {
                "history": [{"messagesAdded": [{"message": {"id": "a"}}]}],
                "historyId": "101",
                "nextPageToken": "t1",
            },
            {
                "history": [
                    {"messagesAdded": [{"message": {"id": "b"}}, {"message": {"id": "a"}}]},
                    {"labelsAdded": [{"message": {"id": "ignored"}}]},
                ],
                "historyId": "105",
            },
        ],
    )

    delta = GmailMailSource(service).changes_since("100")

    assert delta.message_ids == ["a", "b", "a"]  # duplicates kept; DB dedupes
    assert delta.history_id == "105"
    history.list.assert_called_once_with(
        userId="me", startHistoryId="100", historyTypes=["messageAdded"], maxResults=500
    )


def test_changes_since_with_no_changes_keeps_cursor() -> None:
    service = MagicMock()
    _paged(service.users().history(), [{"historyId": "100"}])

    delta = GmailMailSource(service).changes_since("100")

    assert delta.message_ids == []
    assert delta.history_id == "100"


def test_changes_since_404_becomes_history_expired() -> None:
    service = MagicMock()
    service.users().history().list().execute.side_effect = _http_error(404)

    with pytest.raises(HistoryExpiredError):
        GmailMailSource(service).changes_since("1")


def test_changes_since_other_http_errors_propagate() -> None:
    service = MagicMock()
    service.users().history().list().execute.side_effect = _http_error(500)

    with pytest.raises(HttpError):
        GmailMailSource(service).changes_since("1")


def test_iter_message_ids_pages_lazily() -> None:
    service = MagicMock()
    messages = service.users().messages()
    _paged(
        messages,
        [
            {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "t1"},
            {"messages": [{"id": "m3"}]},
        ],
    )

    ids = GmailMailSource(service).iter_message_ids("from:bank.example")

    messages.list.assert_not_called()  # generator: nothing fetched until iterated
    assert list(ids) == ["m1", "m2", "m3"]
    messages.list.assert_called_once_with(userId="me", q="from:bank.example", maxResults=500)


def test_iter_message_ids_empty_result() -> None:
    service = MagicMock()
    _paged(service.users().messages(), [{"resultSizeEstimate": 0}])

    assert list(GmailMailSource(service).iter_message_ids("x")) == []


def test_get_message_fetches_full_format_and_parses() -> None:
    service = MagicMock()
    messages = service.users().messages()
    messages.get().execute.return_value = {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "0",
        "payload": {"mimeType": "text/plain", "headers": [], "body": {}},
    }

    msg = GmailMailSource(service).get_message("m1")

    assert msg.id == "m1"
    messages.get.assert_called_with(userId="me", id="m1", format="full")
