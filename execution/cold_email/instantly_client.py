"""
Layer 3 execution tool: a thin client for the Instantly API **v2**.

V1 IS DEPRECATED. Everything here targets v2, which differs from v1 in three ways that
break naive ports:

  1. Host and prefix: `https://api.instantly.ai/api/v2/...`  (v1 was `/api/v1/...`).
  2. Auth is a **Bearer token in the Authorization header**. V1 passed `api_key` as a query
     parameter. A v1-style call against v2 fails with 401 and no useful message.
  3. Ids are UUIDs returned in the response body, not campaign names.

Verified against Instantly's own OpenAPI document, which is the only machine-readable
source — the human docs are a JS app that returns 404 to any fetch:
    https://developer.instantly.ai/api-reference/openapi.json

CLI usage:
    python execution/cold_email/instantly_client.py --check          # verify the key works
    python execution/cold_email/instantly_client.py --list-accounts  # sending accounts
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every area on sys.path)

BASE_URL = "https://api.instantly.ai"
API_PREFIX = "/api/v2"

# Instantly's documented ceiling for one bulk lead import.
MAX_LEADS_PER_REQUEST = 1000


class InstantlyError(RuntimeError):
    """An API call failed. Carries the status and body so the caller can act on it."""

    def __init__(self, status, body, url):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} from {url}: {body}")


def load_api_key(explicit=None):
    """Explicit key, else INSTANTLY_API_KEY from the environment or .env.

    Loads .env by absolute path rather than from the cwd, which is what keeps these
    scripts working under launchd where there is no shell environment.
    """
    if explicit:
        return explicit.strip()
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "No Instantly API key. Add INSTANTLY_API_KEY=... to .env, or pass --api-key. "
            "Generate one in Instantly under Settings > Integrations > API Keys (V2)."
        )
    return key


class InstantlyClient:
    def __init__(self, api_key=None, base_url=BASE_URL, timeout=60):
        self.api_key = load_api_key(api_key)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            # v2 auth. Not `api_key` in the query string — that is the deprecated v1 shape.
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    # ---------------------------------------------------------------- transport

    def request(self, method, path, payload=None, params=None, retries=3):
        url = f"{self.base_url}{API_PREFIX}{path}"
        last = None
        for attempt in range(retries):
            response = self.session.request(
                method, url, json=payload, params=params, timeout=self.timeout,
            )
            # 429 and 5xx are worth retrying; a 4xx is our bug and retrying just repeats it.
            if response.status_code == 429 or response.status_code >= 500:
                last = response
                wait = float(response.headers.get("Retry-After") or 2 ** attempt)
                print(f"  [instantly] {response.status_code}, retrying in {wait:.0f}s "
                      f"({attempt + 1}/{retries})", flush=True)
                time.sleep(wait)
                continue
            if not response.ok:
                raise InstantlyError(response.status_code, response.text[:800], url)
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {"raw": response.text}
        raise InstantlyError(last.status_code, last.text[:800], url)

    # ---------------------------------------------------------------- endpoints

    def list_accounts(self, limit=100):
        """The connected sending mailboxes. A campaign with none will never send."""
        return self.request("GET", "/accounts", params={"limit": limit})

    def list_campaigns(self, limit=100):
        return self.request("GET", "/campaigns", params={"limit": limit})

    def create_campaign(self, payload):
        """POST /campaigns. `name` and `campaign_schedule` are the only required fields."""
        return self.request("POST", "/campaigns", payload=payload)

    def get_campaign(self, campaign_id):
        return self.request("GET", f"/campaigns/{campaign_id}")

    def add_leads(self, campaign_id, leads, skip_if_in_workspace=True,
                  verify_leads_on_import=False):
        """POST /leads/add — up to 1000 per call, chunked here so the caller needn't care.

        `skip_if_in_workspace` defaults on: re-running the import must not create a second
        copy of a lead who is already in another campaign, which would mail them twice.
        """
        results = []
        for start in range(0, len(leads), MAX_LEADS_PER_REQUEST):
            chunk = leads[start:start + MAX_LEADS_PER_REQUEST]
            results.append(self.request("POST", "/leads/add", payload={
                "campaign_id": campaign_id,
                "leads": chunk,
                "skip_if_in_workspace": skip_if_in_workspace,
                "verify_leads_on_import": verify_leads_on_import,
            }))
        return results

    def list_leads(self, campaign_id, page_size=100):
        """Every lead in a campaign, following the cursor.

        POST, not GET: Instantly's own note says the filter arguments are too complex for
        query parameters, so this one endpoint deviates from their REST shape. Paging is a
        cursor (`next_starting_after`), not an offset, so there is no page count to compute
        and no way to skip ahead.
        """
        leads, cursor, seen = [], None, set()
        while True:
            payload = {"campaign": campaign_id, "limit": page_size}
            if cursor:
                payload["starting_after"] = cursor
            data = self.request("POST", "/leads/list", payload=payload)
            items = data.get("items") or []
            if not items:
                break
            for item in items:
                # A cursor that stops advancing would otherwise loop forever, and this is
                # cheaper to carry than to debug against a live account.
                if item.get("id") in seen:
                    return leads
                seen.add(item.get("id"))
                leads.append(item)
            cursor = data.get("next_starting_after")
            if not cursor or len(items) < page_size:
                break
        return leads

    def update_lead(self, lead_id, payload):
        """PATCH /leads/{id}. Used to rewrite custom variables on leads already imported.

        Send the COMPLETE custom_variables object, never a partial one: the docs don't say
        whether the field merges or replaces, and sending all of it makes the answer
        irrelevant.
        """
        return self.request("PATCH", f"/leads/{lead_id}", payload=payload)

    def activate_campaign(self, campaign_id):
        """POST /campaigns/{id}/activate. Takes no body. THIS STARTS SENDING."""
        return self.request("POST", f"/campaigns/{campaign_id}/activate")

    def pause_campaign(self, campaign_id):
        return self.request("POST", f"/campaigns/{campaign_id}/pause")


def main():
    ap = argparse.ArgumentParser(description="Instantly v2 API client / connectivity check")
    ap.add_argument("--api-key")
    ap.add_argument("--check", action="store_true", help="verify the key authenticates")
    ap.add_argument("--list-accounts", action="store_true")
    ap.add_argument("--list-campaigns", action="store_true")
    args = ap.parse_args()

    try:
        client = InstantlyClient(args.api_key)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1

    try:
        if args.list_campaigns:
            data = client.list_campaigns()
            for item in data.get("items", data if isinstance(data, list) else []):
                print(f"  {item.get('id')}  {item.get('name')}  status={item.get('status')}",
                      flush=True)
            return 0

        # --check and --list-accounts both hinge on the accounts call: it is the cheapest
        # authenticated GET, and "can this key see a mailbox" is the thing worth knowing.
        data = client.list_accounts()
        items = data.get("items", data if isinstance(data, list) else [])
        print(f"OK: authenticated, {len(items)} sending account(s)", flush=True)
        if args.list_accounts:
            for item in items:
                print(f"  {item.get('email')}  warmup={item.get('warmup_status')}  "
                      f"status={item.get('status')}", flush=True)
        print(f"ACCOUNTS={len(items)}", flush=True)
        return 0
    except InstantlyError as exc:
        print(f"ERROR: {exc}", flush=True)
        if exc.status == 401:
            print("  401 usually means a v1 key or the v1 auth style. v2 needs a key from "
                  "Settings > Integrations > API Keys, sent as 'Authorization: Bearer'.",
                  flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
