"""load_credentials: valid / refresh / revoked / unusable token files. Fake values only."""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from app.pipeline.gmail.auth import GmailAuthError, load_credentials


def _write_token(
    path: Path, *, expiry: datetime, refresh_token: str | None = "fake-refresh"
) -> Path:
    path.write_text(
        json.dumps(
            {
                "token": "fake-access",
                "refresh_token": refresh_token,
                "client_id": "fake-client.apps.googleusercontent.com",
                "client_secret": "fake-secret",
                "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ),
        encoding="utf-8",
    )
    return path


FUTURE = datetime.now(UTC) + timedelta(hours=1)
PAST = datetime.now(UTC) - timedelta(hours=1)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(GmailAuthError, match="No Gmail token"):
        load_credentials(tmp_path / "absent.json")


def test_valid_token_is_returned_without_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _write_token(tmp_path / "t.json", expiry=FUTURE)
    before = token.read_text(encoding="utf-8")

    def _no_refresh(self: Credentials, request: Request) -> None:
        raise AssertionError("must not refresh a valid token")

    monkeypatch.setattr(Credentials, "refresh", _no_refresh)

    creds = load_credentials(token)

    assert creds.token == "fake-access"
    assert token.read_text(encoding="utf-8") == before  # file untouched


def test_expired_token_is_refreshed_and_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _write_token(tmp_path / "t.json", expiry=PAST)

    def _fake_refresh(self: Credentials, request: Request) -> None:
        self.token = "new-access"
        self.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)  # naive UTC

    monkeypatch.setattr(Credentials, "refresh", _fake_refresh)

    creds = load_credentials(token)

    assert creds.token == "new-access"
    assert json.loads(token.read_text(encoding="utf-8"))["token"] == "new-access"


def test_revoked_refresh_token_raises_gmail_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _write_token(tmp_path / "t.json", expiry=PAST)

    def _revoked(self: Credentials, request: Request) -> None:
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", _revoked)

    with pytest.raises(GmailAuthError, match="revoked or expired") as exc_info:
        load_credentials(token)
    assert isinstance(exc_info.value.__cause__, RefreshError)  # original error chained


def test_expired_without_refresh_token_raises(tmp_path: Path) -> None:
    token = _write_token(tmp_path / "t.json", expiry=PAST, refresh_token=None)

    with pytest.raises(GmailAuthError, match="no refresh token"):
        load_credentials(token)


@pytest.mark.parametrize("content", ["not json {", '{"token": "only-an-access-token"}'])
def test_unreadable_token_file_raises(tmp_path: Path, content: str) -> None:
    token = tmp_path / "t.json"
    token.write_text(content, encoding="utf-8")

    with pytest.raises(GmailAuthError, match="unreadable"):
        load_credentials(token)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes; runs in CI (Linux)")
def test_refreshed_token_file_is_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _write_token(tmp_path / "t.json", expiry=PAST)
    os.chmod(token, 0o644)  # e.g. a token file copied in with default permissions

    def _fake_refresh(self: Credentials, request: Request) -> None:
        self.token = "new-access"
        self.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", _fake_refresh)

    load_credentials(token)

    assert token.stat().st_mode & 0o777 == 0o600
