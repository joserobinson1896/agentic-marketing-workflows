"""
Layer 2/3 orchestrator: the whole cold campaign in one command.

    scrape  ->  enrich  ->  write copy  ->  build the Instantly campaign (PAUSED)

Each stage is a standalone script and stays runnable on its own; this only sequences them,
passes the artifact from one to the next, and stops the moment a stage fails. Stopping
matters more than sequencing: without it a failed enrichment would hand a stackless CSV to
generation, which would cheerfully spend a hundred model calls writing generic copy.

IT NEVER ACTIVATES THE CAMPAIGN. The campaign is built paused, and starting it is a
separate decision a person makes in the UI. There is no flag here to start sending.

STAGES CAN BE SKIPPED. Re-running after a copy tweak should not re-scrape:
    --from copy      skip scrape and enrich, use the enriched CSV already in the run dir
    --stop-after copy   write the copy and stop; build the campaign later

CLI usage:
    python execution/cold_email/run_cold_pipeline.py --run-name <run> \\
        --source execution/cold_email/sample_leads_100.csv --campaign-name "<name>"
    python execution/cold_email/run_cold_pipeline.py --run-name <run> --from copy --dry-run
"""

import argparse
import csv
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every area on sys.path)
from spend_gate import add_spend_argument, confirm_spend  # noqa: E402  (lives in shared/)
# Imported rather than repeated: the estimate the operator approves has to be priced against
# the model the copy stage will actually use, and a default that drifted in one file only
# would quote them the wrong number.
from build_cold_emails import DEFAULT_MODEL  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_OUTDIR = ROOT / ".tmp" / "cold_email"

STAGES = ["scrape", "enrich", "copy", "campaign"]


def count_leads(path):
    """Rows in the enriched CSV — one paid call each, so this is the estimate's basis.

    utf-8-sig for the same reason every other reader in this pipeline uses it: the scraper
    writes a BOM, and without it the first column name comes back mangled.
    """
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return sum(1 for _ in csv.DictReader(fh))
    except OSError:
        return 0


def run(script, arguments, label):
    """Run one stage, echo it live, and return its KEY=value output lines as a dict.

    Output is streamed rather than captured-then-printed: a stage can take minutes and a
    silent terminal during a hundred model calls is indistinguishable from a hang.
    """
    command = [sys.executable, str(HERE / script), *arguments]
    print(f"\n=== {label} ===", flush=True)
    print(f"$ {' '.join(command[1:])}", flush=True)
    started = time.time()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1)
    values, lines = {}, []
    for line in process.stdout:
        print(line.rstrip(), flush=True)
        lines.append(line)
        if "=" in line and line.split("=", 1)[0].strip().isupper():
            key, _, value = line.strip().partition("=")
            values[key.strip()] = value.strip()
    code = process.wait()
    values["_seconds"] = round(time.time() - started, 1)
    if code != 0:
        raise SystemExit(
            f"\nSTOPPED: {label} failed (exit {code}). Nothing downstream was run, so no "
            f"model calls were spent and no campaign was touched."
        )
    return values


def main():
    ap = argparse.ArgumentParser(description="Run the whole cold campaign pipeline")
    ap.add_argument("--run-name", required=True)
    # scrape
    ap.add_argument("--scrape-provider", default="csv")
    ap.add_argument("--source", help="for --scrape-provider csv: the lead CSV")
    ap.add_argument("--query", help="for a live scrape provider")
    ap.add_argument("--limit", type=int)
    # enrich
    ap.add_argument("--enrich-provider", default="passthrough")
    # copy
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--variants")
    # campaign
    ap.add_argument("--campaign-name")
    ap.add_argument("--text-only", action="store_true", default=True)
    ap.add_argument("--html", dest="text_only", action="store_false",
                    help="send HTML instead of plain text")
    ap.add_argument("--skip-flagged", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the campaign payloads instead of creating anything")
    # control
    ap.add_argument("--from", dest="start", choices=STAGES, default="scrape")
    ap.add_argument("--stop-after", choices=STAGES, default="campaign")
    add_spend_argument(ap)
    args = ap.parse_args()

    start, stop = STAGES.index(args.start), STAGES.index(args.stop_after)
    if start > stop:
        print(f"ERROR: --from {args.start} is after --stop-after {args.stop_after}", flush=True)
        return 1

    base = DEFAULT_OUTDIR / args.run_name
    enriched = base / "enriched_leads.csv"
    results = {}

    if start <= STAGES.index("scrape") <= stop:
        if args.scrape_provider == "csv" and not args.source:
            print("ERROR: --scrape-provider csv needs --source <lead CSV>", flush=True)
            print("  Or wire a real provider: see execution/cold_email/scrape_leads.py",
                  flush=True)
            return 1
        arguments = ["--provider", args.scrape_provider, "--run-name", args.run_name]
        if args.source:
            arguments += ["--source", args.source]
        if args.query:
            arguments += ["--query", args.query]
        if args.limit:
            arguments += ["--limit", str(args.limit)]
        results["scrape"] = run("scrape_leads.py", arguments, "1/4  scrape")

    if start <= STAGES.index("enrich") <= stop:
        results["enrich"] = run(
            "enrich_leads.py",
            ["--run-name", args.run_name, "--provider", args.enrich_provider],
            "2/4  enrich",
        )
        coverage = float(results["enrich"].get("COVERAGE", 0) or 0)
        if coverage == 0:
            raise SystemExit(
                "\nSTOPPED: enrichment resolved a stack for zero leads. Every variant's "
                "pain hook is built from named tools, so the whole batch would be generic. "
                "Wire a real provider (--enrich-provider apollo) or use a lead list that "
                "already carries the column (--enrich-provider passthrough)."
            )

    if start <= STAGES.index("copy") <= stop:
        if not enriched.exists():
            print(f"ERROR: no enriched leads at {enriched}", flush=True)
            print("  Run the earlier stages, or drop the --from flag.", flush=True)
            return 1
        arguments = ["--leads", str(enriched), "--run-name", args.run_name,
                     "--workers", str(args.workers)]
        if args.variants:
            arguments += ["--variants", args.variants]
        # The copy stage is the one that spends, and the gate has to be asked HERE rather
        # than in the child. run() reads the child's stdout through a pipe, and the gate's
        # prompt has no trailing newline, so a child-side question would block the parent's
        # line iterator forever on something the operator never sees. Asking in the parent,
        # which still owns the terminal, and forwarding the answer keeps one approval for
        # the whole run and no silent hang.
        if not confirm_spend(calls=count_leads(enriched), model=DEFAULT_MODEL,
                             label="write personalized cold copy",
                             assume_yes=args.yes_spend):
            return 1
        arguments.append("--yes-spend")
        results["copy"] = run("build_cold_emails.py", arguments, "3/4  write the copy")

    if start <= STAGES.index("campaign") <= stop:
        csv_path = results.get("copy", {}).get("CSV_PATH")
        if not csv_path:
            candidates = sorted(base.glob("*_personalized.csv"))
            if not candidates:
                print(f"ERROR: no personalized CSV in {base}", flush=True)
                return 1
            csv_path = str(candidates[0])
        name = args.campaign_name or f"Cold — {args.run_name}"
        arguments = ["--csv", csv_path, "--name", name]
        if args.text_only:
            arguments.append("--text-only")
        if args.skip_flagged:
            arguments.append("--skip-flagged")
        if args.dry_run:
            arguments.append("--dry-run")
        results["campaign"] = run("create_instantly_campaign.py", arguments,
                                  "4/4  build the campaign (paused)")

    print("\n=== summary ===", flush=True)
    for stage in STAGES:
        if stage in results:
            values = results[stage]
            detail = " ".join(f"{k}={v}" for k, v in values.items()
                              if k.isupper() and not k.endswith("_PATH"))
            print(f"  {stage:<9} {values['_seconds']:>6}s  {detail}", flush=True)
    if "campaign" in results and results["campaign"].get("CAMPAIGN_URL"):
        print(f"\nCAMPAIGN_URL={results['campaign']['CAMPAIGN_URL']}", flush=True)
        print("The campaign is PAUSED. Review it, connect a mailbox, and start it yourself.",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
