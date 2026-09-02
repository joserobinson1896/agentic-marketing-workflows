#!/usr/bin/env python3
"""
Minimal Google Drive uploader used by execution/run_daily_ad_batch.py.

Requires credentials.json (OAuth Desktop client) in the project root and a
token.json produced once by execution/authorize_google_drive.py. Tokens are
refreshed automatically after that — no further interactive login needed.

Scope: drive.file only — this app can only see/manage files it creates
itself, never the rest of the user's Drive.
"""

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = ROOT / "token.json"


def get_credentials():
    if not TOKEN_PATH.exists():
        raise SystemExit(
            f"{TOKEN_PATH} not found. Run "
            "`python3 execution/authorize_google_drive.py` once first to "
            "complete the interactive Google consent flow."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    return creds


def upload_file(local_path, drive_filename, folder_id=None, mime_type="text/html"):
    """Uploads local_path to Drive as drive_filename, returns the file's webViewLink."""
    creds = get_credentials()
    service = build("drive", "v3", credentials=creds)

    metadata = {"name": drive_filename}
    if folder_id:
        metadata["parents"] = [folder_id]

    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=False)
    file = service.files().create(
        body=metadata, media_body=media, fields="id, webViewLink"
    ).execute()
    return file["webViewLink"]
