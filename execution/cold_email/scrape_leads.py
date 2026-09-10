"""
Layer 3 execution tool: source the raw lead list. **This is the seam a real scraper plugs
into.**

Everything downstream of this file is finished and running against real APIs. Only lead
SOURCING is stubbed, because it is the one step that needs a paid data vendor. The
architecture is the same one `enrichment_providers.py` uses, for the same reason: every
provider is one function with one signature, so going live is writing that function and
changing a flag. Nothing else in the pipeline knows or cares where the rows came from.

    fetch(query, limit) -> [row dicts in LEAD_COLUMNS shape]

WHAT A PROVIDER MUST RETURN:
The columns in LEAD_COLUMNS below. `email`, `first_name` and `company_domain` are the only
ones that are load-bearing:
  - `email` addresses the send and is the key Instantly dedupes on.
  - `first_name` opens every email.
  - `company_domain` is what enrichment looks the tech stack up by, and the stack is what
    makes the copy personal. A row without it produces a generic email.
`tech_stack` may be left empty; enrich_leads.py fills it. If your source already has it,
write it as "Tool (Category); Tool (Category)" and enrichment will pass it through.

WIRING A REAL ONE:
Write the fetch function, register it in PROVIDERS, done. The stubs below carry the actual
request shape for each vendor so the next person is not re-reading docs. They raise rather
than return fake data: a scraper that silently invents leads is worse than one that fails.

CLI usage:
    python execution/cold_email/scrape_leads.py --provider csv --source <path> --run-name <run>
    python execution/cold_email/scrape_leads.py --list-providers
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every area on sys.path)

DEFAULT_OUTDIR = ROOT / ".tmp" / "cold_email"

# The contract between this file and the rest of the pipeline. build_cold_emails.py reads
# these names; a provider that returns different ones breaks the chain here rather than
# three steps later, which is the point of naming them in one place.
LEAD_COLUMNS = [
    "full_name", "first_name", "last_name", "job_title", "seniority", "function",
    "email", "email_status", "phone", "linkedin_url",
    "person_city", "person_state", "person_country",
    "company_name", "company_domain", "company_industry", "company_specialties",
    "company_keywords", "company_audience", "company_size", "employee_count",
    "company_city", "company_state", "company_country", "company_linkedin_url",
    "tech_stack", "tech_stack_count",
]

REQUIRED_COLUMNS = ["email", "first_name", "company_domain"]


def _env(name):
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    return os.environ.get(name, "").strip()


def normalize_row(raw):
    """Coerce a provider's row into LEAD_COLUMNS, filling absent keys with ''.

    Providers return what their vendor returns. Normalizing here rather than in each
    provider means a new one only has to map its own field names.
    """
    row = {column: "" for column in LEAD_COLUMNS}
    for key, value in (raw or {}).items():
        if key in row:
            row[key] = "" if value is None else str(value).strip()
    if not row["full_name"]:
        row["full_name"] = " ".join(x for x in (row["first_name"], row["last_name"]) if x)
    if not row["company_domain"] and "@" in row["email"]:
        # Better than an empty domain: enrichment has something to look up, and a
        # freemail address is caught by the check below anyway.
        row["company_domain"] = row["email"].split("@", 1)[1].lower()
    if row["tech_stack"] and not row["tech_stack_count"]:
        row["tech_stack_count"] = str(len([p for p in row["tech_stack"].split(";") if p.strip()]))
    return row


# Addresses that are a person's mailbox rather than a company's. A lead on one of these has
# no company domain to enrich, so the copy would be generic no matter what.
FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "aol.com", "icloud.com", "me.com", "proton.me", "protonmail.com", "gmx.com", "mail.com",
}


def check_rows(rows):
    """Problems worth stopping for. Returns a list; empty means the list is usable."""
    problems = []
    if not rows:
        return ["the provider returned no leads"]

    seen = set()
    for index, row in enumerate(rows, start=1):
        for column in REQUIRED_COLUMNS:
            if not (row.get(column) or "").strip():
                problems.append(f"row {index} ({row.get('email') or 'no email'}): "
                                f"missing {column}")
        email = (row.get("email") or "").strip().lower()
        if email:
            if "@" not in email:
                problems.append(f"row {index}: {email!r} is not an email address")
            elif email in seen:
                problems.append(f"row {index}: duplicate email {email}")
            seen.add(email)
    return problems


def freemail_rows(rows):
    return [r for r in rows
            if (r.get("company_domain") or "").lower() in FREEMAIL]


# ------------------------------------------------------------------------- providers


def fetch_csv(query=None, limit=None, source=None):
    """Read a list someone already exported. The working default, and not a toy.

    Most lead lists arrive as a CSV out of Apollo, Clay, Sales Navigator or a VA's
    spreadsheet, so this is the provider that runs in practice until a vendor is wired.
    """
    if not source:
        raise ValueError("--provider csv needs --source <path to a lead CSV>")
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"no lead CSV at {path}")
    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = [normalize_row(row) for row in csv.DictReader(handle)]
    return rows[:limit] if limit else rows


def fetch_apollo(query=None, limit=None, source=None):
    """Apollo People Search. The best first vendor to wire: it returns the person AND the
    company technographics in one call, so it can serve this file and enrichment both.

        POST https://api.apollo.io/api/v1/mixed_people/search
        header: x-api-key: <APOLLO_API_KEY>
        body:   {"person_titles": [...], "q_organization_domains": [...],
                 "organization_num_employees_ranges": ["50,500"],
                 "page": 1, "per_page": 100}

    Response: `people[]`, each with `email`, `first_name`, `last_name`, `title`,
    `organization.{name,website_url,industry,estimated_num_employees}` and
    `organization.current_technologies[] {uid,name,category}` — which is already the shape
    enrichment_providers.normalize_stack() expects.

    Paginate on `pagination.total_pages`. Emails come back as "email_not_unlocked@domain.com"
    until the contact is revealed, which costs credits: filter those out or you will mail a
    placeholder.
    """
    raise NotImplementedError(
        "Apollo is documented here but not wired. Implement fetch_apollo(), set "
        "APOLLO_API_KEY in .env, and register it in PROVIDERS. The request shape and the "
        "email_not_unlocked trap are in this function's docstring."
    )


def fetch_apify(query=None, limit=None, source=None):
    """An Apify actor run, for LinkedIn-shaped scraping the official APIs don't allow.

        POST https://api.apify.com/v2/acts/<actor-id>/runs?token=<APIFY_TOKEN>
        then poll GET /v2/actor-runs/<runId> until status == "SUCCEEDED"
        then GET /v2/datasets/<defaultDatasetId>/items?format=json

    Actor runs are asynchronous and can take minutes, so this one polls rather than
    returning inline. Map the actor's own field names in normalize_row's vocabulary.
    Check the actor's terms before pointing it at a site.
    """
    raise NotImplementedError(
        "Apify is documented here but not wired. Implement fetch_apify(), set APIFY_TOKEN "
        "in .env, and register it in PROVIDERS. Note it is an async run-then-poll flow, "
        "not a single request."
    )


def fetch_clay(query=None, limit=None, source=None):
    """Clay, when the list is being built and enriched in a Clay table already.

    Clay pushes rather than serves: there is no "give me the table" endpoint on the plans
    most teams have. The practical wiring is a Clay HTTP API column posting rows to a
    webhook this project owns, then reading them back. That means a receiver, and the
    visitor pipeline already has one worth copying (execution/visitor_identification/
    rb2b_site.py) rather than writing a second from scratch.
    """
    raise NotImplementedError(
        "Clay is documented here but not wired, and is push-shaped rather than pull-shaped: "
        "it needs a webhook receiver, not a fetch. See this function's docstring."
    )


PROVIDERS = {
    "csv": fetch_csv,
    "apollo": fetch_apollo,
    "apify": fetch_apify,
    "clay": fetch_clay,
}

# Providers that need no credentials and no network.
OFFLINE_PROVIDERS = {"csv"}


def get_provider(name):
    fetch = PROVIDERS.get(name)
    if fetch is None:
        raise ValueError(f"unknown provider: {name} (have: {', '.join(sorted(PROVIDERS))})")
    return fetch


def write_leads(rows, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=LEAD_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def main():
    ap = argparse.ArgumentParser(description="Source the raw lead list (scraper seam)")
    ap.add_argument("--provider", default="csv",
                    help=f"{' | '.join(sorted(PROVIDERS))} (default: csv)")
    ap.add_argument("--source", help="for --provider csv: the lead CSV to read")
    ap.add_argument("--query", help="for a live provider: the search definition")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--run-name", default="run")
    ap.add_argument("--out", dest="outfile")
    ap.add_argument("--list-providers", action="store_true")
    ap.add_argument("--allow-freemail", action="store_true",
                    help="keep leads whose domain is gmail/outlook/etc (default: drop them)")
    args = ap.parse_args()

    if args.list_providers:
        for name in sorted(PROVIDERS):
            wired = "wired" if name in OFFLINE_PROVIDERS else "NOT WIRED (documented stub)"
            print(f"  {name:<10} {wired}", flush=True)
        return 0

    try:
        fetch = get_provider(args.provider)
        rows = fetch(query=args.query, limit=args.limit, source=args.source)
    except NotImplementedError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1

    free = freemail_rows(rows)
    if free and not args.allow_freemail:
        rows = [r for r in rows if r not in free]
        print(f"  [scrape] dropped {len(free)} lead(s) on a personal email domain "
              f"(no company to enrich); --allow-freemail keeps them", flush=True)

    problems = check_rows(rows)
    if problems:
        print(f"ERROR: the scraped list is not usable ({len(problems)} problem(s)):",
              flush=True)
        for problem in problems[:10]:
            print(f"  - {problem}", flush=True)
        if len(problems) > 10:
            print(f"  ... and {len(problems) - 10} more", flush=True)
        return 1

    outfile = Path(args.outfile) if args.outfile else (
        DEFAULT_OUTDIR / args.run_name / "scraped_leads.csv")
    write_leads(rows, outfile)

    with_stack = sum(1 for r in rows if (r.get("tech_stack") or "").strip())
    print(f"  [scrape] provider={args.provider} rows={len(rows)}", flush=True)
    print(f"  [scrape] {with_stack} already carry a tech stack, "
          f"{len(rows) - with_stack} need enrichment", flush=True)
    print(f"SCRAPED={len(rows)}", flush=True)
    print(f"WITH_STACK={with_stack}", flush=True)
    print(f"LEADS_PATH={outfile}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
