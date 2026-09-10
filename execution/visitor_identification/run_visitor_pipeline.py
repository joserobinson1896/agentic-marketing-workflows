"""
Layer 3 execution tool: run the whole visitor identification pipeline end to end.

    site up -> mock traffic -> RB2B webhooks -> enrich -> draft -> csv + dashboard -> Drive

Boots the Flask site as a subprocess, drives real browser sessions through it, waits for
the resolver's webhooks to settle, then runs each downstream step in order. Every step is
a script that also runs standalone — this only sequences them and reports.

CLI usage:
    python execution/visitor_identification/run_visitor_pipeline.py --run-name demo
    python execution/visitor_identification/run_visitor_pipeline.py --run-name demo --dry-run       # no API spend
    python execution/visitor_identification/run_visitor_pipeline.py --run-name demo --no-upload
    python execution/visitor_identification/run_visitor_pipeline.py --run-name real --match-rate 0.3
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)
from spend_gate import add_spend_argument, confirm_spend  # noqa: E402  (lives in shared/)

# The scripts and config this pipeline shells out to are its own area-mates.
HERE = Path(__file__).resolve().parent

LOG_PATH = ROOT / ".tmp" / "visitor_pipeline.log"


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as fh:
        fh.write(line + "\n")


def run_step(name, argv, capture=True):
    """Run one pipeline script, echo its output, and return the key=value lines it printed."""
    log(f"--- {name}")
    proc = subprocess.run(
        [sys.executable, *argv],
        cwd=ROOT,
        capture_output=capture,
        text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.splitlines():
        if line.strip():
            print(f"    {line}", flush=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{name} failed with exit code {proc.returncode}")
    return dict(re.findall(r"^([A-Z_]+)=(.*)$", out, flags=re.M))


def wait_for_health(url, timeout=25):
    import requests

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(url, timeout=2)
            if resp.ok:
                return resp.json()
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError(f"site never became healthy at {url}")


def wait_for_webhooks(health_url, quiet_for=3.0, timeout=45):
    """Wait until the identified count stops climbing.

    The resolver fires webhooks on a delay from background threads, so 'traffic finished'
    and 'all webhooks landed' are different moments.
    """
    import requests

    last, stable_since, deadline = -1, time.time(), time.time() + timeout
    while time.time() < deadline:
        try:
            count = requests.get(health_url, timeout=2).json().get("identified", 0)
        except Exception:
            count = last
        if count != last:
            last, stable_since = count, time.time()
        elif time.time() - stable_since >= quiet_for:
            return last
        time.sleep(0.5)
    return last


def main():
    ap = argparse.ArgumentParser(description="Run the visitor identification pipeline end to end")
    ap.add_argument("--run-name", default=f"demo_{date.today().isoformat()}")
    ap.add_argument("--visitors", type=int, default=10)
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--engine", choices=["browser", "requests", "auto"], default="auto")
    ap.add_argument("--match-rate", type=float, help="override config; 0.3 ~ real RB2B")
    ap.add_argument("--include-international", action="store_true",
                    help="add 2 non-US sessions to prove the US-only filter drops them")
    ap.add_argument("--dry-run", action="store_true", help="skip paid generation; write prompts only")
    ap.add_argument("--skip-emails", action="store_true")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="wipe any previous run of this name")
    add_spend_argument(ap)
    args = ap.parse_args()

    from rb2b_site import load_config, run_dir

    base = run_dir(args.run_name)
    if args.fresh and base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True, exist_ok=True)

    config_path = HERE / "rb2b_config.json"
    temp_config = None
    if args.match_rate is not None:
        # Don't mutate the checked-in config; hand the site a temp copy for this run.
        cfg = json.loads(config_path.read_text())
        cfg["match_rate"] = args.match_rate
        temp_config = base / "rb2b_config.run.json"
        temp_config.write_text(json.dumps(cfg, indent=2))
        config_path = temp_config

    config = load_config(config_path)
    host = config.get("site_host", "127.0.0.1")
    base_url = f"http://{host}:{args.port}"
    health_url = f"{base_url}/health"

    log(f"run '{args.run_name}' · mode={config.get('mode')} · match_rate={config.get('match_rate')}")

    server_log = base / "site.log"
    server = subprocess.Popen(
        [sys.executable, str(HERE / "rb2b_site.py"),
         "--run-name", args.run_name, "--port", str(args.port), "--config", str(config_path)],
        cwd=ROOT,
        stdout=server_log.open("w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    summary = {}
    try:
        health = wait_for_health(health_url)
        log(f"site up at {base_url}/ (mode={health.get('mode')})")

        traffic = run_step("traffic", [
            str(HERE / "mock_visitors.py"),
            "--visitors", str(args.visitors),
            "--url", base_url,
            "--engine", args.engine,
            *(["--include-international"] if args.include_international else []),
        ])
        summary["sessions"] = traffic.get("VISITORS", str(args.visitors))
        summary["pageviews"] = traffic.get("PAGEVIEWS", "—")
        summary["engine"] = traffic.get("ENGINE", args.engine)

        identified = wait_for_webhooks(health_url)
        log(f"webhooks settled: {identified} identified")
        summary["identified"] = identified
    finally:
        # The site holds the resolver's in-flight threads; only stop it once they've landed.
        if server.poll() is None:
            os.killpg(os.getpgid(server.pid), signal.SIGTERM)
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(server.pid), signal.SIGKILL)
        log("site stopped")

    if not summary.get("identified"):
        log("no visitors were identified — stopping before enrichment")
        print("IDENTIFIED=0", flush=True)
        return 1

    enriched = run_step("enrich", [
        str(HERE / "enrich_visitors.py"), "--run-name", args.run_name,
    ])
    summary["enriched"] = enriched.get("ENRICHED", "0")

    drafted = {}
    if not args.skip_emails:
        # Drafting is the only paid stage, and the approval has to be given to THIS command.
        # run_step captures the child's output and replays it after the child exits, so a
        # gate prompt asked down there would hang with nothing on screen to explain why.
        # Asking here, where the terminal still belongs to us, and forwarding the answer.
        if not args.dry_run:
            from generate_outreach_emails import DEFAULT_MODEL

            leads = int(enriched.get("ENRICHED", "0") or 0)
            if not confirm_spend(calls=leads, label="draft visitor outreach emails",
                                 model=DEFAULT_MODEL, assume_yes=args.yes_spend):
                return 1
        drafted = run_step("draft emails", [
            str(HERE / "generate_outreach_emails.py"),
            "--run-name", args.run_name,
            *(["--dry-run"] if args.dry_run else ["--yes-spend"]),
        ])
    summary["drafted"] = drafted.get("DRAFTED", "0")
    summary["needs_review"] = drafted.get("NEEDS_REVIEW", "0")

    csv_out = run_step("csv export", [
        str(HERE / "export_leads_csv.py"), "--run-name", args.run_name,
    ])
    csv_path = csv_out.get("CSV_PATH")

    dash = run_step("dashboard", [
        str(HERE / "build_leads_dashboard.py"),
        "--run-name", args.run_name,
        "--sessions", str(summary.get("sessions", "")),
        "--pageviews", str(summary.get("pageviews", "")),
        "--engine", str(summary.get("engine", "")),
    ])
    dashboard_path = dash.get("DASHBOARD_PATH")

    folder_link = None
    if not args.no_upload and dashboard_path:
        log("--- upload to Drive")
        try:
            from google_drive_upload import upload_batch

            paths = [p for p in (dashboard_path, csv_path) if p]
            emails_dir = base / "emails"
            if emails_dir.exists():
                paths += sorted(str(p) for p in emails_dir.glob("*.eml"))
            folder_name = f"Identified Visitors — {args.run_name}"
            folder_link, links = upload_batch(paths, folder_name)
            log(f"uploaded {len(links)} file(s) to '{folder_name}'")
        except Exception as exc:
            # Drive auth is optional; a missing token must not void the whole run.
            log(f"Drive upload skipped ({type(exc).__name__}: {exc})")

    if temp_config and temp_config.exists():
        temp_config.unlink()

    print("", flush=True)
    print(f"RUN_NAME={args.run_name}", flush=True)
    print(f"SESSIONS={summary.get('sessions')}", flush=True)
    print(f"PAGEVIEWS={summary.get('pageviews')}", flush=True)
    print(f"ENGINE={summary.get('engine')}", flush=True)
    print(f"IDENTIFIED={summary.get('identified')}", flush=True)
    print(f"ENRICHED={summary.get('enriched')}", flush=True)
    print(f"DRAFTED={summary.get('drafted')}", flush=True)
    print(f"NEEDS_REVIEW={summary.get('needs_review')}", flush=True)
    print(f"DASHBOARD_PATH={dashboard_path}", flush=True)
    print(f"CSV_PATH={csv_path}", flush=True)
    if folder_link:
        print(f"DRIVE_FOLDER_LINK={folder_link}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
