"""
Layer 3 execution tool: enrich identified visitors with their tech stack and ad attribution.

RB2B tells you WHO someone is. It does not tell you why they should care. This step adds
the two things the outreach actually needs:

  1. TECH STACK — which tools the company runs (HubSpot, Stripe, Mailchimp, GA4...).
     For Unified Dashboards this IS the pitch: every tool is another place the numbers
     live. `siloed_sources` counts the ones we'd unify, and `named_tools` picks the two
     or three worth naming in the message.

  2. AD ATTRIBUTION — parsed out of RB2B's "Captured URL" query string, so we know which
     creative and which angle brought them in. No side channel: this is how attribution
     reaches a lead in production too.

Providers are pluggable and live in execution/visitor_identification/enrichment_providers.py:

    mock       fixture data, no credentials needed (default)
    apollo     Apollo.io organization enrichment — returns per-tool categories
    zoominfo   ZoomInfo Enrich Company — deepest technographics, enterprise contract
    builtwith  BuiltWith — detected from the live site
    clearbit   Clearbit / HubSpot Breeze Intelligence
    salesnav   registered but unavailable, with the reason (LinkedIn exposes no
               technographics API — see enrichment_providers.py)

Each implements one function returning [{"tool","category","confidence","source"}, ...].
Nothing downstream reads a provider's raw response, so going live is one function plus a
category mapping — no changes to this file, the drafting or the dashboard.

CLI usage:
    python execution/visitor_identification/enrich_visitors.py --run-name demo
    python execution/visitor_identification/enrich_visitors.py --in .tmp/visitors/demo/identified.jsonl --out .tmp/visitors/demo/enriched.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT, TECH_STACKS  # noqa: E402  (also puts every area on sys.path)

# Data that belongs to this area lives beside it.
HERE = Path(__file__).resolve().parent

from enrichment_providers import PROVIDERS, get_provider  # noqa: E402

STACKS_PATH = TECH_STACKS
AD_REFS_PATH = HERE / "mock_data" / "ad_references.json"

# Named in outreach ahead of the rest: these are the tools a marketing leader recognizes
# as "a place my numbers live". A warehouse or CDP is real, but naming Snowflake at a
# 48-person company sounds like we're reading a report, not paying attention.
HEADLINE_CATEGORIES = [
    "CRM", "Payments", "Ecommerce", "Email Marketing",
    "Advertising", "Web Analytics", "Subscription Billing", "Product Analytics",
]

SENIOR_TITLE_MARKERS = ["chief", "cxo", "cmo", "coo", "ceo", "founder", "vp", "vice president", "head of", "director"]


# ------------------------------------------------------------------------- providers

# The provider seam lives in enrichment_providers.py — that is the file you edit to swap
# the mock for Apollo, ZoomInfo, BuiltWith or Clearbit. Nothing here changes when you do,
# because every provider returns the same normalized [{tool,category,confidence,source}].


def _stack_data():
    return json.loads(STACKS_PATH.read_text())


# ----------------------------------------------------------------------- attribution


def load_ad_index():
    ads = json.loads(AD_REFS_PATH.read_text())["ads"]
    return {ad["ad_id"]: ad for ad in ads}


def parse_attribution(captured_url, referrer, ad_index):
    """Recover the campaign from RB2B's Captured URL, falling back to the referrer."""
    utms = {}
    if captured_url:
        query = parse_qs(urlparse(captured_url).query)
        utms = {k: v[0] for k, v in query.items() if k.startswith("utm_")}

    ad_id = utms.get("utm_content")
    ad = ad_index.get(ad_id) if ad_id else None

    if ad is None:
        host = (urlparse(referrer).netloc.lower() if referrer else "")
        if not referrer:
            ad = ad_index.get("direct")
        elif "google." in host or "bing." in host:
            ad = ad_index.get("organic-search")

    return {
        "ad_id": ad["ad_id"] if ad else None,
        "angle": ad["angle"] if ad else None,
        "ad_headline": ad.get("headline") if ad else None,
        "channel": ad.get("channel") if ad else None,
        "is_paid": bool(ad and ad.get("template")),
        "utm_source": utms.get("utm_source"),
        "utm_medium": utms.get("utm_medium"),
        "utm_campaign": utms.get("utm_campaign"),
        "utm_content": utms.get("utm_content"),
        "landing_page": urlparse(captured_url).path if captured_url else None,
        "referrer": referrer,
    }


# ------------------------------------------------------------------------ enrichment


def pick_named_tools(stack, siloed, limit=3):
    """Choose the tools worth naming in a message.

    One per category, headline categories first, highest confidence within each — so we
    never write "you're running Google Ads and Meta Ads" and call that two data sources.
    """
    by_category = {}
    for tool in stack:
        if tool["tool"] not in siloed:
            continue
        current = by_category.get(tool["category"])
        if current is None or tool["confidence"] > current["confidence"]:
            by_category[tool["category"]] = tool

    ordered = sorted(
        by_category.values(),
        key=lambda t: (
            HEADLINE_CATEGORIES.index(t["category"]) if t["category"] in HEADLINE_CATEGORIES else 99,
            -t["confidence"],
        ),
    )
    return [t["tool"] for t in ordered[:limit]]


def score_lead(record, siloed_count, bi_tool, attribution):
    """0-100 fit score. Weighted toward what actually predicts a good conversation."""
    score = 0

    # More siloed sources = more pain, and a bigger build. The core signal.
    score += min(siloed_count, 6) * 9  # up to 54

    employees = record.get("employee_count") or 0
    if 50 <= employees < 150:
        score += 14   # big enough to hurt, small enough to decide fast
    elif 150 <= employees < 600:
        score += 18   # the sweet spot
    elif employees >= 600:
        score += 11   # likely has in-house data people
    elif employees:
        score += 6

    title = (record.get("title") or "").lower()
    if any(marker in title for marker in SENIOR_TITLE_MARKERS):
        score += 14
    elif title:
        score += 5

    # Already bought a BI seat = budget exists and the problem is acknowledged.
    if bi_tool:
        score += 8
    # Clicked a paid ad about this exact pain, rather than wandering in.
    if attribution.get("is_paid"):
        score += 6

    return max(0, min(100, score))


def enrich_lead(record, provider="mock", ad_index=None, stack_meta=None):
    ad_index = ad_index if ad_index is not None else load_ad_index()
    stack_meta = stack_meta if stack_meta is not None else _stack_data()

    fetch = get_provider(provider)
    stack = fetch(record.get("company_domain"), record)
    data_source_categories = set(stack_meta["data_source_categories"])
    bi_tools = set(stack_meta["bi_tools"])

    siloed = [t["tool"] for t in stack if t["category"] in data_source_categories]
    bi_tool = next((t["tool"] for t in stack if t["tool"] in bi_tools), None)
    attribution = parse_attribution(record.get("captured_url"), record.get("referrer"), ad_index)

    if not stack:
        segment = "no_stack_detected"
    elif bi_tool and len(siloed) >= 4:
        # They own a BI seat but it clearly isn't fed by everything.
        segment = "has_bi_disconnected"
    elif bi_tool:
        segment = "has_bi"
    elif len(siloed) >= 4:
        segment = "many_sources_no_bi"
    else:
        segment = "few_sources_no_bi"

    return {
        # identity, straight from RB2B
        "linkedin_url": record.get("linkedin_url"),
        "first_name": record.get("first_name"),
        "full_name": record.get("full_name"),
        "title": record.get("title"),
        "company_name": record.get("company_name"),
        "company_domain": record.get("company_domain"),
        "business_email": record.get("business_email"),
        "city": record.get("city"),
        "state": record.get("state"),
        "industry": record.get("industry"),
        "employee_count": record.get("employee_count"),
        "estimated_revenue": record.get("estimated_revenue"),
        "seen_at": record.get("seen_at"),
        # what enrichment added
        "attribution": attribution,
        "tech_stack": stack,
        "siloed_sources": siloed,
        "siloed_source_count": len(siloed),
        "bi_tool": bi_tool,
        "named_tools": pick_named_tools(stack, set(siloed)),
        "segment": segment,
        "lead_score": score_lead(record, len(siloed), bi_tool, attribution),
        "enrichment_provider": provider,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    ap = argparse.ArgumentParser(description="Enrich identified visitors with tech stack + attribution")
    ap.add_argument("--run-name", default="demo")
    ap.add_argument("--in", dest="infile", help="defaults to .tmp/visitors/<run>/identified.jsonl")
    ap.add_argument("--out", dest="outfile", help="defaults to .tmp/visitors/<run>/enriched.json")
    ap.add_argument(
        "--provider", default=None,
        help="mock (default) | apollo | zoominfo | builtwith | clearbit — see enrichment_providers.py",
    )
    args = ap.parse_args()

    from rb2b_site import load_config, run_dir

    config = load_config()
    provider = args.provider or config.get("enrichment_provider", "mock")

    base = run_dir(args.run_name)
    infile = Path(args.infile) if args.infile else base / "identified.jsonl"
    outfile = Path(args.outfile) if args.outfile else base / "enriched.json"

    if not infile.exists():
        print(f"ERROR: no identified visitors at {infile}", flush=True)
        print("ENRICHED=0", flush=True)
        return 1

    records = [json.loads(line) for line in infile.read_text().splitlines() if line.strip()]
    ad_index = load_ad_index()
    stack_meta = _stack_data()

    leads, no_stack, failed = [], [], []
    for record in records:
        try:
            lead = enrich_lead(record, provider=provider, ad_index=ad_index, stack_meta=stack_meta)
        except (NotImplementedError, RuntimeError) as exc:
            # Provider-level failure — an unwired provider, a missing API key. It will fail
            # identically for every lead, so stop now. Logging it ten times and then writing
            # an empty enriched.json would destroy a previous good run's output.
            print(f"ERROR: provider '{provider}' is unavailable: {exc}", flush=True)
            print("ENRICHED=0", flush=True)
            return 1
        except Exception as exc:  # one bad lead must not sink the batch
            print(f"  [enrich] FAILED {record.get('full_name')}: {exc}", flush=True)
            failed.append(record.get("full_name"))
            continue
        if not lead["tech_stack"]:
            no_stack.append(lead["company_name"])
        leads.append(lead)

    if records and not leads:
        # Every lead failed individually. Still a systemic problem, and still not a reason
        # to overwrite the output with nothing.
        print(f"ERROR: all {len(records)} leads failed to enrich; leaving {outfile} untouched",
              flush=True)
        print("ENRICHED=0", flush=True)
        return 1

    leads.sort(key=lambda lead: lead["lead_score"], reverse=True)
    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text(json.dumps(leads, indent=2))

    for lead in leads:
        tools = ", ".join(lead["named_tools"]) or "—"
        print(
            f"  [{lead['lead_score']:>3}] {lead['full_name']:<18} {lead['company_name']:<24} "
            f"{lead['siloed_source_count']} sources · {tools}",
            flush=True,
        )
    if no_stack:
        print(f"  [enrich] no stack detected for: {', '.join(no_stack)}", flush=True)
    if failed:
        print(f"  [enrich] failed: {', '.join(failed)}", flush=True)

    print(f"ENRICHED={len(leads)}", flush=True)
    print(f"ENRICHED_PATH={outfile}", flush=True)
    print(f"PROVIDER={provider}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
