"""
Layer 3 execution tool: rewrite the personalized copy on leads ALREADY in an Instantly
campaign, without re-importing them.

WHY THIS EXISTS AND add_leads DOESN'T DO IT:
`POST /leads/add` is an import. Run against emails already in the campaign it either skips
them (when skip_if_in_workspace is on, which it must be, or the same person gets mailed
twice) or creates duplicates. Neither updates anything. Changing the copy on an existing
lead is `PATCH /leads/{id}`, and that needs the lead's UUID, which only comes back from
`POST /leads/list`. So the job is three steps, not one:

    campaign id -> list every lead -> map email to UUID -> PATCH each with the new copy

WHAT IT SENDS:
The COMPLETE custom_variables object from create_instantly_campaign.build_lead_payload, not
just the fields that changed. Instantly's docs don't say whether custom_variables merges
into the existing object or replaces it, and sending the whole thing makes the answer
irrelevant. Importing that builder rather than re-deriving the variable names here is what
stops the two scripts drifting apart: the campaign's sequence references {{email_subject}}
and {{personalized_email_html}} by name, so a rename in one place and not the other sends
100 emails with an empty body.

IT DOES NOT ACTIVATE ANYTHING. It also refuses to touch a campaign that is currently
sending unless forced, because rewriting copy underneath an in-flight send means some
prospects get the old version and some the new, with no record of which.

CLI usage:
    python execution/cold_email/update_instantly_leads.py --dry-run
    python execution/cold_email/update_instantly_leads.py --campaign-id <id>
    python execution/cold_email/update_instantly_leads.py --csv .tmp/cold_email/<run>/...csv
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every area on sys.path)

from instantly_client import InstantlyClient, InstantlyError  # noqa: E402
from create_instantly_campaign import (  # noqa: E402
    build_lead_payload,
    check_rows,
    load_rows,
)

DEFAULT_CSV = ROOT / ".tmp" / "cold_email" / "mach100_v2" / "mock_leads_100_personalized.csv"

# Instantly's campaign status enum. 1 is a campaign actively sending; everything else is
# some flavour of not-sending (0 draft, 2 paused, 3 completed, 4 running-subsequences).
STATUS_ACTIVE = 1
STATUS_NAMES = {0: "draft", 1: "ACTIVE (sending)", 2: "paused", 3: "completed"}

# PATCH is one call per lead, so a 100-lead update is 100 calls. A small gap keeps a batch
# well under Instantly's rate limit; the client retries a 429 anyway, but avoiding one is
# cheaper than recovering from it.
PAUSE_BETWEEN_CALLS = 0.12


# Instantly folds a custom variable whose name collides with one of its own lead fields
# into that field, camelCased, and drops the name we sent. Verified against a live campaign:
# `job_title` comes back as `jobTitle` and `payload["job_title"]` does not exist. It costs
# nothing here (the sequence references neither), but without this map every diff reports
# job_title as changed forever, and a future template using {{job_title}} would render empty.
# The variables that carry the copy — personalized_email, personalized_email_html,
# email_subject, email_variant, named_tools — collide with nothing and round-trip verbatim.
INSTANTLY_SYSTEM_FIELDS = {
    "job_title": "jobTitle",
    "first_name": "firstName",
    "last_name": "lastName",
    "company_name": "companyName",
}


def fields_that_changed(before, after):
    """Which custom variables actually differ, for the per-lead log line.

    `before` comes off the lead's `payload` field, which is where Instantly stores custom
    variables on a listed lead, alongside its own camelCased copies of the system fields.
    """
    before = before or {}
    changed = []
    for key, value in (after or {}).items():
        stored = before.get(key, before.get(INSTANTLY_SYSTEM_FIELDS.get(key, key)))
        if stored != value:
            changed.append(key)
    return changed


def main():
    ap = argparse.ArgumentParser(
        description="Rewrite personalized copy on leads already in an Instantly campaign")
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    ap.add_argument("--campaign-id", help="defaults to the only campaign, if there is one")
    ap.add_argument("--campaign-name", help="find the campaign by name instead of id")
    ap.add_argument("--api-key")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would change; makes only read calls")
    ap.add_argument("--force", action="store_true",
                    help="update even if the campaign is actively sending")
    ap.add_argument("--skip-flagged", action="store_true",
                    help="ignore rows flagged email_needs_review=yes")
    ap.add_argument("--limit", type=int, help="only the first N rows (smoke test)")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: no CSV at {csv_path}", flush=True)
        print("  Generate it first: python execution/cold_email/build_cold_emails.py "
              "--run-name <run>", flush=True)
        return 1

    rows = load_rows(csv_path, skip_flagged=args.skip_flagged)
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print(f"ERROR: {csv_path} has no rows to send", flush=True)
        return 1

    # The same guard the campaign builder uses. An empty body here would blank out copy
    # that is currently correct, which is worse than never having run.
    problems = check_rows(rows)
    if problems:
        print("ERROR: the CSV is not safe to push:", flush=True)
        for problem in problems[:10]:
            print(f"  - {problem}", flush=True)
        return 1

    try:
        client = InstantlyClient(args.api_key)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1

    try:
        campaign_id = args.campaign_id
        if not campaign_id:
            data = client.list_campaigns()
            items = data.get("items", data if isinstance(data, list) else [])
            if args.campaign_name:
                items = [c for c in items if c.get("name") == args.campaign_name]
            if len(items) != 1:
                print(f"ERROR: found {len(items)} campaigns; pass --campaign-id", flush=True)
                for item in items:
                    print(f"  {item.get('id')}  {item.get('name')}", flush=True)
                return 1
            campaign_id = items[0].get("id")
            print(f"  [instantly] campaign {campaign_id} ({items[0].get('name')!r})",
                  flush=True)

        campaign = client.get_campaign(campaign_id)
        status = campaign.get("status")
        label = STATUS_NAMES.get(status, f"status={status}")
        print(f"  [instantly] status: {label}", flush=True)
        if status == STATUS_ACTIVE and not args.force:
            print("ERROR: this campaign is actively sending. Rewriting copy mid-send means "
                  "some prospects get the old version and some the new, with no record of "
                  "which. Pause it first, or pass --force.", flush=True)
            return 1

        print("  [instantly] fetching existing leads...", flush=True)
        existing = client.list_leads(campaign_id)
        by_email = {(l.get("email") or "").strip().lower(): l for l in existing}
        print(f"  [instantly] {len(existing)} lead(s) in the campaign", flush=True)

    except InstantlyError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1

    # Reconcile both directions before writing anything, so the operator sees the whole
    # picture rather than discovering a mismatch 60 calls in.
    payloads = {}
    missing = []
    for row in rows:
        payload = build_lead_payload(row)
        email = payload["email"].strip().lower()
        if email not in by_email:
            missing.append(payload["email"])
            continue
        payloads[email] = payload

    orphans = sorted(set(by_email) - set(payloads) - {e.lower() for e in missing})

    if missing:
        print(f"  [instantly] {len(missing)} CSV row(s) are NOT in the campaign and will be "
              f"skipped: {', '.join(missing[:5])}"
              f"{' ...' if len(missing) > 5 else ''}", flush=True)
        print("    (add them with create_instantly_campaign.py --leads-only)", flush=True)
    if orphans:
        print(f"  [instantly] {len(orphans)} lead(s) in the campaign are not in this CSV and "
              f"will be left alone: {', '.join(orphans[:5])}"
              f"{' ...' if len(orphans) > 5 else ''}", flush=True)

    if args.dry_run:
        sample = next(iter(payloads.values()), None)
        print(f"  [dry-run] would PATCH {len(payloads)} lead(s)", flush=True)
        if sample:
            lead = by_email[sample["email"].strip().lower()]
            changed = fields_that_changed(lead.get("payload"), sample["custom_variables"])
            print(f"  [dry-run] first lead {sample['email']} -> id {lead.get('id')}", flush=True)
            print(f"  [dry-run] changing: {', '.join(changed) or '(nothing)'}", flush=True)
            print(json.dumps({"custom_variables": sample["custom_variables"]},
                             indent=2)[:1200], flush=True)
        print(f"UPDATED=0", flush=True)
        print(f"WOULD_UPDATE={len(payloads)}", flush=True)
        return 0

    updated, failed, unchanged = 0, [], 0
    for email, payload in payloads.items():
        lead = by_email[email]
        lead_id = lead.get("id")
        body = {
            "personalization": payload.get("personalization", ""),
            "custom_variables": payload["custom_variables"],
        }
        changed = fields_that_changed(lead.get("payload"), payload["custom_variables"])
        try:
            client.update_lead(lead_id, body)
        except InstantlyError as exc:
            print(f"  [update] FAILED {payload['email']}: {exc}", flush=True)
            failed.append(payload["email"])
            continue
        updated += 1
        if not changed:
            unchanged += 1
        subject = payload["custom_variables"].get("email_subject", "")
        print(f"  [update] {payload['email']:<42} {subject[:38]!r} "
              f"({len(changed)} var{'s' if len(changed) != 1 else ''} changed)", flush=True)
        time.sleep(PAUSE_BETWEEN_CALLS)

    print(f"UPDATED={updated}", flush=True)
    print(f"ALREADY_CURRENT={unchanged}", flush=True)
    print(f"SKIPPED_NOT_IN_CAMPAIGN={len(missing)}", flush=True)
    print(f"LEFT_ALONE_NOT_IN_CSV={len(orphans)}", flush=True)
    if failed:
        print(f"FAILED={len(failed)}: {', '.join(failed[:5])}", flush=True)
        return 1
    print(f"CAMPAIGN_URL=https://app.instantly.ai/app/campaign/{campaign_id}", flush=True)
    print("The campaign was NOT activated. Nothing has sent.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
