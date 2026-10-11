"""GmailMailSource against a mocked Gmail service: paging, 404 translation, delegation."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.pipeline.gmail.client import RATE_LIMIT_WAITS, GmailMailSource
from app.pipeline.gmail.source import HistoryExpiredError, MessageParseError


def _http_error(status: int, reason: str | None = None) -> HttpError:
    body = {"error": {"message": "x", "errors": [{"reason": reason}]}} if reason else {}
    return HttpError(httplib2.Response({"status": str(status)}), json.dumps(body).encode())


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


def test_get_message_that_wont_parse_raises_message_parse_error() -> None:
    service = MagicMock()
    service.users().messages().get().execute.return_value = {
        "id": "m1",
        "internalDate": "1700000000000",  # threadId missing
        "payload": {},
    }

    with pytest.raises(MessageParseError) as caught:
        GmailMailSource(service).get_message("m1")

    assert caught.value.message_id == "m1"
    assert caught.value.received_at is not None
    assert caught.value.received_at.year == 2023  # internalDate still recovered
    assert isinstance(caught.value.__cause__, ValueError)


def test_get_message_parse_error_without_readable_date() -> None:
    service = MagicMock()
    service.users().messages().get().execute.return_value = {"id": "m1", "internalDate": "x"}

    with pytest.raises(MessageParseError) as caught:
        GmailMailSource(service).get_message("m1")

    assert caught.value.received_at is None


def test_get_message_http_errors_are_not_parse_errors() -> None:
    # A Gmail/network failure must abort the poll (retry next tick), not mark mail failed.
    service = MagicMock()
    service.users().messages().get().execute.side_effect = _http_error(500)

    with pytest.raises(HttpError):
        GmailMailSource(service).get_message("m1")


def _get(service: MagicMock) -> MagicMock:
    get: MagicMock = service.users().messages().get()
    return get


_RAW = {"id": "m1", "threadId": "t1", "internalDate": "0", "payload": {}}


@pytest.mark.parametrize(
    "error",
    [
        _http_error(403, "rateLimitExceeded"),  # Gmail's "errors" format
        _http_error(403, "RATE_LIMIT_EXCEEDED"),  # google.rpc ErrorInfo format
        _http_error(429),
    ],
)
def test_rate_limit_waits_then_succeeds(error: HttpError) -> None:
    service = MagicMock()
    _get(service).execute.side_effect = [error, error, _RAW]
    waits: list[float] = []

    message = GmailMailSource(service, sleep=waits.append).get_message("m1")

    assert message.id == "m1"
    assert waits == list(RATE_LIMIT_WAITS[:2])  # waited twice, at growing intervals


def test_rate_limit_gives_up_after_the_last_wait() -> None:
    service = MagicMock()
    _get(service).execute.side_effect = _http_error(403, "rateLimitExceeded")
    waits: list[float] = []

    with pytest.raises(HttpError):
        GmailMailSource(service, sleep=waits.append).get_message("m1")

    assert waits == list(RATE_LIMIT_WAITS)
    assert _get(service).execute.call_count == len(RATE_LIMIT_WAITS) + 1


def test_permission_403_is_not_retried() -> None:
    # A 403 without a rate-limit reason (e.g. scope missing) won't fix itself by waiting.
    service = MagicMock()
    _get(service).execute.side_effect = _http_error(403, "insufficientPermissions")
    waits: list[float] = []

    with pytest.raises(HttpError):
        GmailMailSource(service, sleep=waits.append).get_message("m1")

    assert waits == []
