"""
Layer 3 execution tool: fill the `tech_stack` column on a scraped lead list.

WHY THIS STEP DECIDES THE COPY:
Every variant's pain hook is built from the prospect's actual tools, and the grounding check
rejects a draft that names fewer than two of them. A lead with no detected stack falls into
the `no_stack_detected` segment, where the prompt forbids naming any tool at all, and the
resulting email is generic. **Enrichment coverage is the ceiling on copy quality**, so this
script reports it loudly rather than letting a thin run look like a good one.

It reuses execution/shared/enrichment_providers.py — the same seam the visitor pipeline
enriches through, moved to shared/ when this area started needing it. Adding a vendor there
serves both pipelines.

PROVIDERS:
  passthrough  the CSV already carries tech_stack (an Apollo/Clay export usually does).
               Validates and normalizes the column instead of paying to re-fetch it.
  mock         the offline fixture. Only knows the visitor demo domains, so on a real
               scrape it resolves almost nothing — which the coverage report will say.
  apollo       real, needs APOLLO_API_KEY. Best single choice: per-tool categories.
  builtwith    real, needs BUILTWITH_API_KEY. Best signal for what is on their site now.
  clearbit     real, partial categories.
  zoominfo     documented, needs a JWT exchange, not wired.

CLI usage:
    python execution/cold_email/enrich_leads.py --run-name <run>
    python execution/cold_email/enrich_leads.py --run-name <run> --provider apollo
    python execution/cold_email/enrich_leads.py --in <csv> --out <csv> --provider passthrough
"""

import argparse
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every area on sys.path)

from enrichment_providers import (  # noqa: E402  (lives in shared/)
    OFFLINE_PROVIDERS,
    PROVIDERS,
    get_provider,
    normalize_stack,
)
from scrape_leads import LEAD_COLUMNS, write_leads  # noqa: E402

DEFAULT_OUTDIR = ROOT / ".tmp" / "cold_email"

# Lookups are network-bound and independent per company, so they parallelize the same way
# generation does. Vendors rate limit harder than the model, so this is deliberately lower.
DEFAULT_WORKERS = 4

# Below this, the batch will read generically no matter how good the frameworks are, and
# saying so before the copy is written is cheaper than saying it after.
COVERAGE_WARN = 0.6


def stack_to_column(stack):
    """Our stack list back into the CSV's "Tool (Category); Tool (Category)" column."""
    return "; ".join(f"{entry['tool']} ({entry['category']})" for entry in stack)


def column_to_stack(value):
    """Parse the column a previous run (or the vendor's own export) already wrote."""
    from build_cold_emails import parse_stack
    return parse_stack(value)


def enrich_rows(rows, provider, workers=DEFAULT_WORKERS):
    """Fill tech_stack on every row. Returns (rows, per-row detected counts).

    A per-company cache matters more than it looks: 20 of the 100 companies on the reference
    list have two contacts, so a naive loop pays a vendor twice for the same domain and can
    get two different answers, which would give colleagues contradictory emails.
    """
    fetch = get_provider(provider)
    cache = {}
    counts = [0] * len(rows)

    def work(index):
        row = rows[index]
        domain = (row.get("company_domain") or "").strip().lower()
        if not domain:
            return
        if domain not in cache:
            try:
                cache[domain] = fetch(domain, row) or []
            except (NotImplementedError, RuntimeError):
                raise  # systemic: unwired provider or a missing key. Abort the run.
            except Exception as exc:
                print(f"  [enrich] {domain}: {exc}", flush=True)
                cache[domain] = []
        stack = cache[domain]
        row["tech_stack"] = stack_to_column(stack)
        row["tech_stack_count"] = str(len(stack))
        counts[index] = len(stack)

    # One pass over unique domains first, so the cache is warm and the pool isn't racing to
    # fetch the same company several times.
    domains = sorted({(r.get("company_domain") or "").strip().lower() for r in rows} - {""})
    index_by_domain = {}
    for index, row in enumerate(rows):
        index_by_domain.setdefault((row.get("company_domain") or "").strip().lower(), index)
    first_pass = [index_by_domain[d] for d in domains if d in index_by_domain]

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(work, first_pass))
    for index in range(len(rows)):
        work(index)
    return rows, counts


def main():
    ap = argparse.ArgumentParser(description="Fill tech_stack on a scraped lead list")
    ap.add_argument("--run-name", default="run")
    ap.add_argument("--in", dest="infile", help="defaults to .tmp/cold_email/<run>/scraped_leads.csv")
    ap.add_argument("--out", dest="outfile", help="defaults to .tmp/cold_email/<run>/enriched_leads.csv")
    ap.add_argument("--provider", default="passthrough",
                    help=f"passthrough | {' | '.join(sorted(PROVIDERS))} (default: passthrough)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--list-providers", action="store_true")
    ap.add_argument("--allow-empty", action="store_true",
                    help="write the file even if no lead resolved a stack (default: refuse)")
    args = ap.parse_args()

    if args.list_providers:
        print(f"  {'passthrough':<12} wired (uses the column already in the CSV)", flush=True)
        for name in sorted(PROVIDERS):
            wired = "wired, no credentials" if name in OFFLINE_PROVIDERS else "needs an API key"
            print(f"  {name:<12} {wired}", flush=True)
        return 0

    base = DEFAULT_OUTDIR / args.run_name
    infile = Path(args.infile) if args.infile else base / "scraped_leads.csv"
    outfile = Path(args.outfile) if args.outfile else base / "enriched_leads.csv"
    if not infile.exists():
        print(f"ERROR: no scraped leads at {infile}", flush=True)
        print("  Run scrape_leads.py first.", flush=True)
        return 1

    with open(infile, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        print(f"ERROR: {infile} has no rows", flush=True)
        return 1

    started = time.time()
    if args.provider == "passthrough":
        # Re-normalize rather than trust the column: a vendor export writes categories in
        # its own vocabulary, and siloed_sources keys off ours.
        counts = []
        for row in rows:
            stack = column_to_stack(row.get("tech_stack"))
            normalized = normalize_stack(
                [{"tool": e["tool"], "category": e["category"]} for e in stack],
                source_label="passthrough",
            )
            row["tech_stack"] = stack_to_column(normalized)
            row["tech_stack_count"] = str(len(normalized))
            counts.append(len(normalized))
    else:
        try:
            rows, counts = enrich_rows(rows, args.provider, workers=args.workers)
        except (NotImplementedError, RuntimeError) as exc:
            # A systemic failure must never write a file: an empty tech_stack column over a
            # good one silently destroys the copy for the whole batch.
            print(f"ERROR: {exc}", flush=True)
            print("  Nothing was written.", flush=True)
            return 1
    elapsed = time.time() - started

    resolved = sum(1 for c in counts if c)
    coverage = resolved / len(rows) if rows else 0

    # Refuse to write a file where NOTHING resolved. A run that fails systemically (wrong
    # provider, dead key, a vendor returning empty for every domain) would otherwise
    # overwrite a good enriched CSV with a stackless one, and the damage only shows up as
    # a batch of generic copy much later. Same lesson as the visitor pipeline's
    # "a per-item guard must not swallow a systemic failure".
    if rows and not resolved and not args.allow_empty:
        print(f"ERROR: provider {args.provider!r} resolved a stack for 0 of {len(rows)} "
              f"leads. Nothing was written, so any existing {outfile.name} is intact.",
              flush=True)
        print("  Every variant's pain hook is built from named tools, so this batch would "
              "be generic. Try --provider apollo or builtwith, or --provider passthrough "
              "if the CSV already carries the column. --allow-empty overrides.", flush=True)
        return 1

    rows = [{column: row.get(column, "") for column in LEAD_COLUMNS} for row in rows]
    write_leads(rows, outfile)
    total = sum(counts)
    print(f"  [enrich] provider={args.provider} in {elapsed:.1f}s", flush=True)
    print(f"  [enrich] {resolved}/{len(rows)} leads have a detected stack "
          f"({coverage:.0%}), {total / max(resolved, 1):.1f} tools each on average",
          flush=True)
    if coverage < COVERAGE_WARN:
        print(f"  [enrich] WARNING: coverage is {coverage:.0%}. Every variant's pain hook "
              f"is built from named tools, and a lead with no stack gets a generic email. "
              f"Consider --provider apollo or builtwith before generating copy.", flush=True)
    print(f"ENRICHED={len(rows)}", flush=True)
    print(f"WITH_STACK={resolved}", flush=True)
    print(f"COVERAGE={coverage:.2f}", flush=True)
    print(f"LEADS_PATH={outfile}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
