#!/usr/bin/env python3
"""
One-time interactive Google OAuth authorization for the daily ad-batch
uploader. Run this once by hand (it opens a browser for consent):

    python3 execution/shared/authorize_google_drive.py

It reads credentials.json (OAuth Desktop client, project root) and writes
token.json (also project root) holding a refresh token — after this,
execution/ad_creator/run_daily_ad_batch.py can upload to Drive with no further
interaction, including from an unattended launchd job.
"""

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)
CREDENTIALS_PATH = ROOT / "credentials.json"
TOKEN_PATH = ROOT / "token.json"


def main():
    if not CREDENTIALS_PATH.exists():
        raise SystemExit(f"{CREDENTIALS_PATH} not found — drop your OAuth client's credentials.json there first.")

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json())
    print(f"Wrote {TOKEN_PATH} — Drive uploads are now authorized for unattended runs.")


if __name__ == "__main__":
    main()
