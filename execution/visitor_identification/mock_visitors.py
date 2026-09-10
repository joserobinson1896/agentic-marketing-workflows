"""
Layer 3 execution tool: drive mock visitors through the live local site as real traffic.

>>> MOCK COMPONENT. Delete on cutover — real humans replace this. <<<

This does not fabricate pageviews. It opens real browser sessions against the running
Flask site, which means the RB2B pixel shim actually executes in a real JS runtime and
beacons the pageview itself, exactly as RB2B's script would. Each visitor gets a fresh
browser context, so each gets its own localStorage and therefore its own visitor id —
the same way distinct people look distinct to a tracking pixel.

Visitors arrive from mock ad references (execution/visitor_identification/mock_data/ad_references.json) with
UTMs on the entry URL and a matching referrer. Those UTMs ride through to the webhook
inside RB2B's "Captured URL" field, which is how ad attribution reaches the lead in
production too — no side channel.

Engines:
  browser   Playwright + Chromium. The pixel really runs. Default.
  requests  No browser available. Fetches pages over real HTTP, then posts the beacon
            to /mock/collect on the pixel's behalf. The site and receiver are still
            exercised; only the JS execution is simulated. Falls back here automatically.

CLI usage:
    python execution/visitor_identification/mock_visitors.py --visitors 10 --url http://127.0.0.1:5000
    python execution/visitor_identification/mock_visitors.py --visitors 12 --include-international
    python execution/visitor_identification/mock_visitors.py --visitors 10 --engine requests
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)

# Data that belongs to this area lives beside it.
HERE = Path(__file__).resolve().parent

AD_REFS_PATH = HERE / "mock_data" / "ad_references.json"
PAGES = ["/", "/sample-dashboard", "/pricing"]


def load_ad_refs(path=None):
    return json.loads(Path(path or AD_REFS_PATH).read_text())["ads"]


def build_journey(rng, ad):
    """Entry page carries the UTMs; the rest of the journey is clean internal navigation."""
    entry = "/"
    query = urlencode(ad.get("utm", {}))
    entry_url = f"{entry}?{query}" if query else entry
    depth = rng.choice([1, 2, 2, 3, 3, 4])
    rest = rng.sample(PAGES[1:], k=min(depth - 1, len(PAGES) - 1))
    return [entry_url] + rest


def run_browser(base_url, journeys, headed=False, verbose=True):
    from playwright.sync_api import sync_playwright

    from render_ads_to_png import chromium_executable  # shared Chromium resolution

    executable = chromium_executable()
    fired = 0
    with sync_playwright() as p:
        browser = (
            p.chromium.launch(headless=not headed, executable_path=executable)
            if executable
            else p.chromium.launch(headless=not headed)
        )
        try:
            for i, (ad, pages) in enumerate(journeys, start=1):
                # A fresh context per visitor = fresh localStorage = a distinct visitor id.
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    locale="en-US",
                )
                page = context.new_page()
                try:
                    for j, path in enumerate(pages):
                        url = base_url.rstrip("/") + path
                        referer = ad.get("referrer") if j == 0 else None
                        page.goto(url, referer=referer, wait_until="load", timeout=20000)
                        page.wait_for_timeout(350)  # let the beacon leave
                        fired += 1
                    if verbose:
                        print(
                            f"  [visitor {i:02d}] {ad['ad_id']:<16} "
                            f"{len(pages)} page(s) via {ad.get('channel')}"
                        )
                finally:
                    context.close()
        finally:
            browser.close()
    return fired


def run_requests(base_url, journeys, verbose=True):
    """No-browser fallback: real HTTP for the pages, hand-posted beacon for the pixel."""
    import requests

    fired = 0
    for i, (ad, pages) in enumerate(journeys, start=1):
        session = requests.Session()
        visitor_id = f"v-req-{i:03d}"
        for j, path in enumerate(pages):
            url = base_url.rstrip("/") + path
            headers = {"User-Agent": "Mozilla/5.0 (mock-visitor)"}
            if j == 0 and ad.get("referrer"):
                headers["Referer"] = ad["referrer"]
            session.get(url, headers=headers, timeout=10)
            session.post(
                base_url.rstrip("/") + "/mock/collect",
                json={
                    "script_id": "MOCK",
                    "captured_url": url,
                    "referrer": ad.get("referrer") if j == 0 else base_url,
                    "visitor_id": visitor_id,
                    "title": "Unified Dashboards",
                },
                timeout=10,
            )
            fired += 1
        if verbose:
            print(f"  [visitor {i:02d}] {ad['ad_id']:<16} {len(pages)} page(s) (requests engine)")
    return fired


def main():
    ap = argparse.ArgumentParser(description="Send mock visitors through the local site")
    ap.add_argument("--visitors", type=int, default=10)
    ap.add_argument("--url", default="http://127.0.0.1:5000")
    ap.add_argument("--engine", choices=["browser", "requests", "auto"], default="auto")
    ap.add_argument(
        "--include-international",
        action="store_true",
        help="send 2 extra sessions so the resolver's US-only filter has something to reject",
    )
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--headed", action="store_true", help="watch the browser work")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    ads = load_ad_refs()
    total = args.visitors + (2 if args.include_international else 0)

    journeys = []
    for i in range(total):
        ad = ads[i % len(ads)]
        journeys.append((ad, build_journey(rng, ad)))

    print(f"VISITORS={total}")
    print(f"TARGET={args.url}")

    engine = args.engine
    if engine in ("auto", "browser"):
        try:
            fired = run_browser(args.url, journeys, headed=args.headed)
            engine = "browser"
        except Exception as exc:
            if args.engine == "browser":
                raise
            print(f"  [warn] browser engine unavailable ({type(exc).__name__}: {exc})")
            print("  [warn] falling back to the requests engine — the pixel JS will not execute")
            fired = run_requests(args.url, journeys)
            engine = "requests"
    else:
        fired = run_requests(args.url, journeys)

    time.sleep(2.0)  # let the resolver's delayed webhooks land
    print(f"ENGINE={engine}")
    print(f"PAGEVIEWS={fired}")


if __name__ == "__main__":
    main()
