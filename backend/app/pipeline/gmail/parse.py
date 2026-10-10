"""Turn a Gmail API message (format="full") into a MailMessage. Pure: no network, no I/O."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from email.message import Message as _MimeHeaders
from typing import TYPE_CHECKING

from app.pipeline.gmail.source import MailMessage

if TYPE_CHECKING:
    # Exist only in google-api-python-client-stubs (type_check_only), not at runtime.
    from googleapiclient._apis.gmail.v1.schemas import Message, MessagePart

_DEFAULT_CHARSET = "utf-8"


def parse_message(raw: Message) -> MailMessage:
    """Build a MailMessage from users.messages.get(format="full") output.

    Raises ValueError if Gmail's required fields (id, threadId, internalDate) are missing.
    """
    msg_id = raw.get("id")
    thread_id = raw.get("threadId")
    internal_date = raw.get("internalDate")
    if not msg_id or not thread_id or not internal_date:
        raise ValueError("Gmail message missing id, threadId or internalDate")

    payload = raw.get("payload", {})
    headers = tuple((h.get("name", ""), h.get("value", "")) for h in payload.get("headers", []))

    return MailMessage(
        id=msg_id,
        thread_id=thread_id,
        received_at=datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC),
        headers=headers,
        text_body=_find_body(payload, "text/plain"),
        html_body=_find_body(payload, "text/html"),
    )


def _find_body(part: MessagePart, mime_type: str) -> str | None:
    """Depth-first search for the first inline part of `mime_type`, decoded to str."""
    body = part.get("body", {})
    is_attachment = bool(part.get("filename")) or "attachmentId" in body
    if part.get("mimeType") == mime_type and not is_attachment:
        data = body.get("data")
        if data:
            return _decode(data, _charset(part))
    for child in part.get("parts", []):
        found = _find_body(child, mime_type)
        if found is not None:
            return found
    return None


def _charset(part: MessagePart) -> str:
    """Charset from the part's Content-Type header, e.g. 'text/plain; charset="iso-8859-1"'."""
    for h in part.get("headers", []):
        if h.get("name", "").lower() == "content-type":
            mime = _MimeHeaders()
            mime["Content-Type"] = h.get("value", "")
            return mime.get_content_charset() or _DEFAULT_CHARSET
    return _DEFAULT_CHARSET


def _decode(data: str, charset: str) -> str:
    """Gmail body data is base64url (often without padding) of the part's raw bytes."""
    raw_bytes = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        return raw_bytes.decode(charset, errors="replace")
    except LookupError:  # unknown charset name, e.g. "x-bogus"
        return raw_bytes.decode(_DEFAULT_CHARSET, errors="replace")
