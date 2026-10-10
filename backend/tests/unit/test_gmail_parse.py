"""parse_message on synthetic Gmail payloads (no real mail in fixtures)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from app.pipeline.gmail.parse import parse_message

if TYPE_CHECKING:
    from googleapiclient._apis.gmail.v1.schemas import Message


def _b64(text: str, charset: str = "utf-8") -> str:
    """Encode like Gmail does: base64url, padding stripped."""
    return base64.urlsafe_b64encode(text.encode(charset)).decode().rstrip("=")


def _part(mime: str, text: str = "", charset: str = "utf-8", **extra: Any) -> dict[str, Any]:
    return {
        "mimeType": mime,
        "headers": [{"name": "Content-Type", "value": f'{mime}; charset="{charset}"'}],
        "body": {"data": _b64(text, charset)} if text else {},
        **extra,
    }


def _message(payload: dict[str, Any]) -> Message:
    msg: Message = {
        "id": "18f0a1b2c3d4e5f6",
        "threadId": "18f0a1b2c3d4e5f6",
        "internalDate": "1760000000000",  # 2025-10-09T08:53:20Z
        "payload": payload,  # type: ignore[typeddict-item]
    }
    return msg


def test_single_part_text_plain() -> None:
    payload = _part("text/plain", "Rs 500 debited")
    payload["headers"] = [
        {"name": "From", "value": "alerts@bank.example"},
        {"name": "Subject", "value": "Debit alert"},
    ]

    msg = parse_message(_message(payload))

    assert msg.id == "18f0a1b2c3d4e5f6"
    assert msg.text_body == "Rs 500 debited"
    assert msg.html_body is None
    assert msg.header("from") == "alerts@bank.example"


def test_received_at_is_internal_date_in_utc() -> None:
    msg = parse_message(_message(_part("text/plain", "x")))

    assert msg.received_at == datetime(2025, 10, 9, 8, 53, 20, tzinfo=UTC)


def test_nested_alternative_inside_mixed_skips_attached_file() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "headers": [],
        "body": {},
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "headers": [],
                "body": {},
                "parts": [
                    _part("text/plain", "plain version"),
                    _part("text/html", "<p>html version</p>"),
                ],
            },
            # an attached .txt file must not be mistaken for the body
            _part("text/plain", "attached file", filename="statement.txt"),
        ],
    }

    msg = parse_message(_message(payload))

    assert msg.text_body == "plain version"
    assert msg.html_body == "<p>html version</p>"


def test_attached_file_listed_first_is_not_the_body() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "headers": [],
        "body": {},
        "parts": [
            _part("text/plain", "attached file", filename="notes.txt"),
            _part("text/plain", "real body"),
        ],
    }

    assert parse_message(_message(payload)).text_body == "real body"


def test_large_attachment_without_inline_data_is_ignored() -> None:
    attachment = _part("text/plain", filename="big.txt")
    attachment["body"] = {"attachmentId": "ANGjdJ8", "size": 900000}
    payload = {"mimeType": "multipart/mixed", "headers": [], "body": {}, "parts": [attachment]}

    assert parse_message(_message(payload)).text_body is None


def test_charsets_are_decoded() -> None:
    utf8 = parse_message(_message(_part("text/plain", "Café ₹", charset="utf-8")))
    latin = parse_message(_message(_part("text/plain", "Café", charset="iso-8859-1")))

    assert utf8.text_body == "Café ₹"
    assert latin.text_body == "Café"


def test_unknown_charset_falls_back_to_utf8() -> None:
    payload = _part("text/plain", "hello")
    payload["headers"] = [{"name": "Content-Type", "value": 'text/plain; charset="x-bogus"'}]

    assert parse_message(_message(payload)).text_body == "hello"


def test_repeated_headers_are_all_kept_in_order() -> None:
    payload = _part("text/plain", "x")
    payload["headers"] = [
        {"name": "Authentication-Results", "value": "mx.google.com; dmarc=pass"},
        {"name": "Authentication-Results", "value": "other.example; spf=fail"},
    ]

    msg = parse_message(_message(payload))

    assert msg.header_all("authentication-results") == [
        "mx.google.com; dmarc=pass",
        "other.example; spf=fail",
    ]


@pytest.mark.parametrize("missing", ["id", "threadId", "internalDate"])
def test_missing_required_field_raises(missing: str) -> None:
    raw: dict[str, Any] = dict(_message(_part("text/plain", "x")))
    del raw[missing]

    with pytest.raises(ValueError, match="missing"):
        parse_message(raw)  # type: ignore[arg-type]
