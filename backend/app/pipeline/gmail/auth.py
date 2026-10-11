"""Gmail OAuth: one-time consent (host only) and credential loading (worker)."""

import argparse
import os
import tempfile
from pathlib import Path
from typing import Final

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES: Final = ["https://www.googleapis.com/auth/gmail.readonly"]

# backend/app/pipeline/gmail/auth.py -> parents[4] is the repo root
_SECRETS = Path(__file__).resolve().parents[4] / "secrets"


class GmailAuthError(RuntimeError):
    """Token missing, revoked or expired: a human must re-run consent."""


def _save_token(creds: Credentials, token_path: Path) -> None:
    # Atomic: write a temp file in the SAME directory, then os.replace() it over the token.
    # A crash mid-write leaves the old token intact instead of a half-written one, and
    # readers see either the old or the new file, never a mix. (Same directory = same
    # filesystem, which rename needs; that's why the worker mounts a directory, not a file.)
    # mkstemp creates the file 0o600 (owner only); Windows ignores mode bits (uses ACLs).
    token_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=token_path.parent, prefix=f".{token_path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:  # owns fd from here: always closed
            f.write(creds.to_json())
            f.flush()
            os.fsync(f.fileno())  # bytes on disk before the rename makes them visible
        os.replace(tmp_name, token_path)  # atomic on POSIX and Windows
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)  # don't leave temp files behind
        raise


def load_credentials(token_path: Path) -> Credentials:
    if not token_path.exists():
        raise GmailAuthError(
            f"No Gmail token at {token_path}. Run: uv run python -m app.pipeline.gmail.auth"
        )
    try:
        creds: Credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except ValueError as e:  # corrupt JSON (JSONDecodeError) or missing required keys
        raise GmailAuthError(f"Gmail token at {token_path} is unreadable; re-run consent") from e
    if creds.valid:
        return creds  # access token still good (the common case)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())  # trade refresh token for a new access token
        except RefreshError as e:  # revoked, or 7-day Testing-mode expiry
            raise GmailAuthError("Gmail token revoked or expired; re-run consent") from e
        _save_token(creds, token_path)  # persist the new access token
        return creds
    raise GmailAuthError("Gmail token has no refresh token; re-run consent")


def run_consent(client_secret_path: Path, token_path: Path) -> Credentials:
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
    creds: Credentials = flow.run_local_server(port=0, open_browser=True)
    _save_token(creds, token_path)
    return creds


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail OAuth consent + smoke test")
    parser.add_argument("--client-secret", type=Path, default=_SECRETS / "client_secret.json")
    parser.add_argument("--token", type=Path, default=_SECRETS / "gmail" / "gmail_token.json")
    parser.add_argument(
        "--force", action="store_true", help="re-run consent even if a token exists"
    )
    args = parser.parse_args()

    creds: Credentials | None = None
    if not args.force:
        try:
            creds = load_credentials(args.token)
        except GmailAuthError as e:
            print(f"{e} -> starting consent")
    if creds is None:
        creds = run_consent(args.client_secret, args.token)

    # Smoke test: print the COUNT only, never label names (they can reveal personal info).
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    print(f"OK - Gmail reachable, {len(labels)} labels")


if __name__ == "__main__":
    main()
