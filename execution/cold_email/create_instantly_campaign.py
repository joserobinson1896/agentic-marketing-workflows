"""
Layer 3 execution tool: build the Instantly campaign from the personalized lead CSV.

The whole design is one idea: the copy is already written, one email per lead, so the
campaign sequence holds no copy at all. Its body is a single variable —

    subject: {{email_subject}}
    body:    {{personalized_email_html}}

— and each lead carries its own values as Instantly custom variables. One campaign, one
step, 100 genuinely different emails. Nothing is spun, merged or templated at send time.

IT DOES NOT START SENDING. The campaign is created paused and stays that way until someone
passes --activate, because activating mails real people and cannot be recalled.

INSTANTLY API V2 ONLY — v1 is deprecated. See instantly_client.py for the auth difference
that silently 401s a v1-style call.

CLI usage:
    python execution/cold_email/create_instantly_campaign.py --dry-run    # payloads, no calls
    python execution/cold_email/create_instantly_campaign.py --name "Mach 100 — Cold"
    python execution/cold_email/create_instantly_campaign.py --campaign-id <id> --leads-only
"""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every area on sys.path)

from instantly_client import InstantlyClient, InstantlyError  # noqa: E402

DEFAULT_CSV = ROOT / ".tmp" / "cold_email" / "mach100" / "mock_leads_100_personalized.csv"

# Instantly's timezone field is an enum and AMERICA/NEW_YORK IS NOT IN IT. Eastern time is
# spelled America/Detroit. Passing the tz name everyone expects fails validation.
DEFAULT_TIMEZONE = "America/Detroit"

# Keys "0".."6". The spec's own example sets 0-4 true and 5-6 false, which only reads as a
# working week if 0 is Monday. Inferred, not documented — eyeball the schedule in the UI
# after the first create rather than trusting this blind.
WEEKDAYS = {"0": True, "1": True, "2": True, "3": True, "4": True, "5": False, "6": False}


def build_schedule(timezone=DEFAULT_TIMEZONE, start="09:00", end="17:00"):
    return {
        "schedules": [{
            "name": "Business hours",
            "timing": {"from": start, "to": end},
            "days": dict(WEEKDAYS),
            "timezone": timezone,
        }]
    }


def build_campaign_payload(name, sending_accounts=None, timezone=DEFAULT_TIMEZONE,
                           daily_limit=30, email_gap=10, text_only=False):
    """The campaign whose entire body is one per-lead variable."""
    body_variable = "{{personalized_email}}" if text_only else "{{personalized_email_html}}"

    payload = {
        "name": name,
        "campaign_schedule": build_schedule(timezone),
        "sequences": [{
            "steps": [{
                "type": "email",
                # Delay before the NEXT step. One step, so nothing follows it — but the
                # field is required, so it is present and inert.
                "delay": 3,
                "variants": [{
                    "subject": "{{email_subject}}",
                    "body": body_variable,
                }],
            }],
        }],
        "daily_limit": daily_limit,
        "email_gap": email_gap,
        "text_only": text_only,
        "stop_on_reply": True,
        # A reply from anyone at a company stops the whole company. Cheap insurance
        # against two people at one domain getting the same pitch after one replied.
        "stop_for_company": True,
        "stop_on_auto_reply": True,
        # Both off deliberately. A tracking pixel and rewritten links are the two things
        # most likely to put a cold first-touch in spam, and open rates stopped meaning
        # anything once mail privacy protection started prefetching them.
        "link_tracking": False,
        "open_tracking": False,
        # CAN-SPAM needs a working opt-out. The header carries it so the copy doesn't have
        # to end in a footer, which is what keeps these reading like a person wrote them.
        "insert_unsubscribe_header": True,
    }
    if sending_accounts:
        payload["email_list"] = sending_accounts
    return payload


def build_lead_payload(row):
    """One CSV row -> one Instantly lead carrying its own finished email.

    custom_variables values must be string/number/boolean/null — Instantly rejects objects
    and arrays outright, so everything here is flattened to a string.
    """
    return {
        "email": (row.get("email") or "").strip(),
        "first_name": (row.get("first_name") or "").strip(),
        "last_name": (row.get("last_name") or "").strip(),
        "company_name": (row.get("company_name") or "").strip(),
        "website": (row.get("company_domain") or "").strip(),
        "phone": (row.get("phone") or "").strip(),
        # Instantly's native personalization field, usable as {{personalization}}.
        "personalization": (row.get("personalized_email") or "").strip(),
        "custom_variables": {
            "personalized_email": (row.get("personalized_email") or "").strip(),
            "personalized_email_html": (row.get("personalized_email_html") or "").strip(),
            "email_subject": (row.get("email_subject") or "").strip(),
            "email_variant": (row.get("email_variant") or "").strip(),
            "named_tools": (row.get("tech_stack") or "").strip()[:500],
            "job_title": (row.get("job_title") or "").strip(),
        },
    }


def load_rows(path, skip_flagged=False):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if skip_flagged:
        rows = [r for r in rows if (r.get("email_needs_review") or "").lower() != "yes"]
    return rows


def check_rows(rows, text_only=False):
    """Refuse to build a campaign that would mail blanks. Returns a list of problems."""
    problems = []
    body_column = "personalized_email" if text_only else "personalized_email_html"
    seen = set()
    for i, row in enumerate(rows, start=2):  # +2: 1-indexed, past the header
        email = (row.get("email") or "").strip()
        who = email or f"row {i}"
        if not email or "@" not in email:
            problems.append(f"{who}: missing or malformed email")
        elif email.lower() in seen:
            problems.append(f"{who}: duplicate email in the CSV")
        else:
            seen.add(email.lower())
        if not (row.get(body_column) or "").strip():
            problems.append(f"{who}: empty {body_column}")
        if not (row.get("email_subject") or "").strip():
            problems.append(f"{who}: empty email_subject")
    return problems


def main():
    ap = argparse.ArgumentParser(description="Create the Instantly v2 campaign for a lead CSV")
    ap.add_argument("--csv", default=str(DEFAULT_CSV), help="the personalized lead CSV")
    ap.add_argument("--name", default="Mach 100 — Unified Dashboards Cold")
    ap.add_argument("--api-key")
    ap.add_argument("--campaign-id", help="add leads to an existing campaign instead")
    ap.add_argument("--leads-only", action="store_true", help="skip campaign creation")
    ap.add_argument("--sending-accounts", help="comma-separated mailboxes to send from")
    ap.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    ap.add_argument("--daily-limit", type=int, default=30)
    ap.add_argument("--email-gap", type=int, default=10, help="minutes between sends")
    ap.add_argument("--text-only", action="store_true",
                    help="send plain text, using {{personalized_email}} instead of the HTML")
    ap.add_argument("--skip-flagged", action="store_true",
                    help="exclude leads whose email_needs_review is yes")
    ap.add_argument("--activate", action="store_true",
                    help="START SENDING once built. Off by default, on purpose.")
    ap.add_argument("--dry-run", action="store_true", help="print payloads, call nothing")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: no personalized CSV at {csv_path}", flush=True)
        print("  run execution/cold_email/build_cold_emails.py first", flush=True)
        return 1

    rows = load_rows(csv_path, skip_flagged=args.skip_flagged)
    if not rows:
        print("ERROR: no leads to add", flush=True)
        return 1

    problems = check_rows(rows, text_only=args.text_only)
    if problems:
        print(f"ERROR: {len(problems)} lead(s) would be mailed a blank or bad email:", flush=True)
        for problem in problems[:15]:
            print(f"  - {problem}", flush=True)
        print("  refusing to build the campaign", flush=True)
        return 1

    flagged = sum(1 for r in rows if (r.get("email_needs_review") or "").lower() == "yes")
    if flagged and not args.skip_flagged:
        print(f"  WARNING: {flagged} lead(s) have email_needs_review=yes and are included. "
              f"Re-run with --skip-flagged to hold them back.", flush=True)

    sending_accounts = (
        [a.strip() for a in args.sending_accounts.split(",") if a.strip()]
        if args.sending_accounts else None
    )
    campaign_payload = build_campaign_payload(
        args.name, sending_accounts, args.timezone,
        args.daily_limit, args.email_gap, args.text_only,
    )
    lead_payloads = [build_lead_payload(row) for row in rows]

    if args.dry_run:
        print(json.dumps(campaign_payload, indent=2), flush=True)
        print(f"\n  [dry-run] {len(lead_payloads)} leads. First one:", flush=True)
        print(json.dumps(lead_payloads[0], indent=2)[:1400], flush=True)
        split = Counter(r.get("email_variant") for r in rows)
        print(f"\n  [dry-run] variant split: {dict(split)}", flush=True)
        print("LEADS=0", flush=True)
        return 0

    try:
        client = InstantlyClient(args.api_key)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1

    try:
        campaign_id = args.campaign_id
        if not args.leads_only and not campaign_id:
            campaign = client.create_campaign(campaign_payload)
            campaign_id = campaign.get("id")
            if not campaign_id:
                print(f"ERROR: no campaign id in response: {campaign}", flush=True)
                return 1
            print(f"  [instantly] created campaign {campaign_id!r} — {args.name}", flush=True)
        if not campaign_id:
            print("ERROR: --leads-only needs --campaign-id", flush=True)
            return 1

        responses = client.add_leads(campaign_id, lead_payloads)
        added = sum(r.get("leads_uploaded", r.get("total_sent", 0)) or 0 for r in responses)
        skipped = sum(r.get("skipped_count", 0) or 0 for r in responses)
        blocked = sum(r.get("in_blocklist_count", 0) or 0 for r in responses)
        invalid = sum(r.get("invalid_email_count", 0) or 0 for r in responses)
        print(f"  [instantly] submitted {len(lead_payloads)} leads in {len(responses)} "
              f"request(s)", flush=True)
        print(f"  [instantly] uploaded={added} skipped={skipped} blocked={blocked} "
              f"invalid={invalid}", flush=True)

        if args.activate:
            client.activate_campaign(campaign_id)
            print("  [instantly] CAMPAIGN ACTIVATED — it is now sending", flush=True)
        else:
            print("  [instantly] campaign left PAUSED. Review it in the UI, then activate "
                  "there or re-run with --activate.", flush=True)

        print(f"CAMPAIGN_ID={campaign_id}", flush=True)
        print(f"LEADS={len(lead_payloads)}", flush=True)
        print(f"ACTIVATED={'yes' if args.activate else 'no'}", flush=True)
        print(f"CAMPAIGN_URL=https://app.instantly.ai/app/campaign/{campaign_id}", flush=True)
        return 0
    except InstantlyError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
