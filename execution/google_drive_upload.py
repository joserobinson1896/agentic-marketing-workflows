#!/usr/bin/env python3
"""
Google Drive uploader for the ad batches.

Credentials are resolved in this order, so the same code works on a laptop and
in an ephemeral cloud sandbox that can't carry secret files:

  1. GOOGLE_SERVICE_ACCOUNT_JSON  — the full service-account JSON, inline.
     Best for unattended/cloud runs. The target Drive folder must be shared
     with the service account's client_email, since a service account has no
     Drive storage of its own.
  2. GOOGLE_OAUTH_TOKEN_JSON      — the contents of a token.json, inline.
     Uploads land in the user's own Drive, owned by the user.
  3. token.json in the project root — the local-machine path, produced once by
     execution/authorize_google_drive.py.

Scope is drive.file: this app can only see and manage files it created itself,
never the rest of the user's Drive.
"""

import json
import mimetypes
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = ROOT / "token.json"
FOLDER_MIME = "application/vnd.google-apps.folder"


def get_credentials():
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        return ServiceAccountCredentials.from_service_account_info(
            json.loads(sa_json), scopes=SCOPES
        )

    token_json = os.environ.get("GOOGLE_OAUTH_TOKEN_JSON")
    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
        return creds

    raise RuntimeError(
        "No Drive credentials found. Set GOOGLE_SERVICE_ACCOUNT_JSON or "
        "GOOGLE_OAUTH_TOKEN_JSON, or run `python3 execution/authorize_google_drive.py` "
        "once to create token.json."
    )


def get_service():
    return build("drive", "v3", credentials=get_credentials())


def ensure_folder(service, name, parent_id=None):
    """Find a folder by name (creating it if absent) and return its id.

    Only folders this app created are visible under the drive.file scope, so
    this finds the batch folders it made on previous runs rather than piling up
    duplicates — but it will not see a folder you made by hand in the Drive UI.
    Pass that folder's id explicitly as parent_id instead.
    """
    query = [f"name = '{name}'", f"mimeType = '{FOLDER_MIME}'", "trashed = false"]
    if parent_id:
        query.append(f"'{parent_id}' in parents")
    result = service.files().list(
        q=" and ".join(query), fields="files(id, name)", pageSize=1
    ).execute()
    files = result.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def upload_file(local_path, drive_filename=None, folder_id=None, mime_type=None, service=None):
    """Upload one file (text or binary) and return its webViewLink."""
    service = service or get_service()
    local_path = Path(local_path)
    drive_filename = drive_filename or local_path.name
    if mime_type is None:
        mime_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"

    metadata = {"name": drive_filename}
    if folder_id:
        metadata["parents"] = [folder_id]

    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=False)
    file = service.files().create(
        body=metadata, media_body=media, fields="id, webViewLink"
    ).execute()
    return file["webViewLink"]


def upload_batch(paths, folder_name, parent_id=None):
    """Upload many files into one (found-or-created) folder.

    Returns (folder_link, [(filename, link), ...]).
    """
    service = get_service()
    folder_id = ensure_folder(service, folder_name, parent_id=parent_id)
    links = []
    for path in paths:
        link = upload_file(path, folder_id=folder_id, service=service)
        links.append((Path(path).name, link))
    folder_link = f"https://drive.google.com/drive/folders/{folder_id}"
    return folder_link, links
