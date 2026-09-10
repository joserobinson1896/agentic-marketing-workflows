"""
Layer 3 execution tool: flatten a run into one CSV — every lead, its enrichment, and its draft.

One row per identified lead, joined on company domain. Written for reading in a spreadsheet:
the identity and scoring columns come first, then enrichment, then attribution, then the
draft itself (subject and body last, since they are the long ones).

Emitted with a UTF-8 BOM so Excel opens accented characters and the em dashes in generated
copy correctly instead of showing mojibake. Numbers stay unquoted so sorting by lead_score
or employee_count works without a text-to-columns dance.

CLI usage:
    python execution/visitor_identification/export_leads_csv.py --run-name demo
    python execution/visitor_identification/export_leads_csv.py --run-name demo --out ~/Desktop/leads.csv
"""

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)

COLUMNS = [
    # who
    "lead_score", "full_name", "first_name", "title", "company_name", "company_domain",
    "business_email", "linkedin_url", "city", "state", "industry",
    "employee_count", "estimated_revenue",
    # what we found
    "segment", "siloed_source_count", "siloed_sources", "bi_tool", "named_tools",
    "tech_stack", "enrichment_provider",
    # how they arrived
    "ad_id", "ad_angle", "ad_headline", "channel", "utm_source", "utm_medium",
    "utm_campaign", "utm_content", "landing_page", "referrer", "seen_at",
    # what we wrote
    "subject", "body", "word_count", "entry_angle", "cta_shape",
    "needs_review", "problems", "attempts", "eml_file",
]


def flatten(lead, draft):
    attribution = lead.get("attribution") or {}
    draft = draft or {}
    body = draft.get("body") or ""
    stack = "; ".join(
        f"{t['tool']} ({t['category']}, {t['confidence']})" for t in lead.get("tech_stack") or []
    )
    return {
        "lead_score": lead.get("lead_score"),
        "full_name": lead.get("full_name"),
        "first_name": lead.get("first_name"),
        "title": lead.get("title"),
        "company_name": lead.get("company_name"),
        "company_domain": lead.get("company_domain"),
        "business_email": lead.get("business_email"),
        "linkedin_url": lead.get("linkedin_url"),
        "city": lead.get("city"),
        "state": lead.get("state"),
        "industry": lead.get("industry"),
        "employee_count": lead.get("employee_count"),
        "estimated_revenue": lead.get("estimated_revenue"),
        "segment": lead.get("segment"),
        "siloed_source_count": lead.get("siloed_source_count"),
        "siloed_sources": ", ".join(lead.get("siloed_sources") or []),
        "bi_tool": lead.get("bi_tool"),
        "named_tools": ", ".join(lead.get("named_tools") or []),
        "tech_stack": stack,
        "enrichment_provider": lead.get("enrichment_provider"),
        "ad_id": attribution.get("ad_id"),
        "ad_angle": attribution.get("angle"),
        "ad_headline": attribution.get("ad_headline"),
        "channel": attribution.get("channel"),
        "utm_source": attribution.get("utm_source"),
        "utm_medium": attribution.get("utm_medium"),
        "utm_campaign": attribution.get("utm_campaign"),
        "utm_content": attribution.get("utm_content"),
        "landing_page": attribution.get("landing_page"),
        "referrer": attribution.get("referrer"),
        "seen_at": lead.get("seen_at"),
        "subject": draft.get("subject"),
        "body": body,
        "word_count": len(body.split()) if body else 0,
        "entry_angle": draft.get("entry_angle"),
        "cta_shape": draft.get("cta_shape"),
        "needs_review": draft.get("needs_review"),
        "problems": "; ".join(draft.get("problems") or []),
        "attempts": draft.get("attempts"),
        "eml_file": Path(draft["eml_path"]).name if draft.get("eml_path") else None,
    }


def main():
    ap = argparse.ArgumentParser(description="Export a run to a single CSV")
    ap.add_argument("--run-name", default="demo")
    ap.add_argument("--enriched")
    ap.add_argument("--drafts")
    ap.add_argument("--out", help="defaults to .tmp/visitors/<run>/leads.csv")
    args = ap.parse_args()

    from rb2b_site import run_dir

    base = run_dir(args.run_name)
    enriched_path = Path(args.enriched) if args.enriched else base / "enriched.json"
    drafts_path = Path(args.drafts) if args.drafts else base / "emails" / "drafts.json"
    out_path = Path(args.out).expanduser() if args.out else base / "leads.csv"

    if not enriched_path.exists():
        print(f"ERROR: no enriched leads at {enriched_path}", flush=True)
        print("CSV_ROWS=0", flush=True)
        return 1

    leads = json.loads(enriched_path.read_text())
    drafts = json.loads(drafts_path.read_text()) if drafts_path.exists() else []
    by_domain = {d.get("company_domain"): d for d in drafts}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: Excel assumes the local codepage without a BOM and mangles em dashes.
    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            writer.writerow(flatten(lead, by_domain.get(lead.get("company_domain"))))

    missing = sum(1 for lead in leads if lead.get("company_domain") not in by_domain)
    print(f"CSV_PATH={out_path}", flush=True)
    print(f"CSV_ROWS={len(leads)}", flush=True)
    print(f"CSV_COLUMNS={len(COLUMNS)}", flush=True)
    if missing:
        print(f"ROWS_WITHOUT_DRAFT={missing}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
