"""
Layer 3 execution tool: render the Unified Dashboards dashboard.

Takes the semantic layer (every number, already computed and trap-corrected by
build_semantic_layer.py) and injects it into template.html, which holds the iOS-styled
UI, the hand-drawn SVG charts and the AI analyst.

The split matters: this file renders and never computes. If a figure looks wrong on
the page, it is wrong in build_semantic_layer.py, and test_semantic_layer.py should
have caught it.

CLI usage:
    python execution/dashboard/build_dashboard.py
    python execution/dashboard/build_dashboard.py --rebuild     # re-run the semantic layer first
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template.html"
# sample() accepts 65,536 bytes of prompt. The brief is assembled in the browser from
# the aggregates below, so this is the ceiling the aggregate section has to respect.
MAX_PROMPT_BYTES = 65_536


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--layer", default=str(ROOT / ".tmp" / "dashboard" / "semantic_layer.json"))
    ap.add_argument("--out", default=str(ROOT / ".tmp" / "dashboard" / "dashboard.html"))
    ap.add_argument("--rebuild", action="store_true", help="re-run build_semantic_layer.py first")
    args = ap.parse_args()

    layer_path = Path(args.layer)
    if args.rebuild or not layer_path.exists():
        subprocess.run([sys.executable, str(HERE / "build_semantic_layer.py"),
                        "--out", str(layer_path)], check=True)

    layer = json.loads(layer_path.read_text(encoding="utf-8"))
    template = TEMPLATE.read_text(encoding="utf-8")
    if "__DATA__" not in template:
        raise SystemExit("template.html has no __DATA__ placeholder")

    # Refuse to ship a dashboard built from data that failed its own integrity checks.
    failed = [c["name"] for c in layer["canaries"] if not c["pass"]]
    if failed:
        raise SystemExit("integrity canary failed: " + ", ".join(failed))

    payload = json.dumps(layer, separators=(",", ":"))
    # The payload sits inside a <script> block, so a literal </script> or an HTML
    # comment opener inside any string would end it early.
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--")

    html = template.replace("__DATA__", payload)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    # The aggregates the AI brief is assembled from — everything except the row-level
    # tables, which reach Claude through the queryAccounts tool instead.
    brief_keys = ("kpis", "channels", "campaigns", "silo_bands", "retention_by_sources",
                  "retention_by_channel", "churn_reasons", "noshow_bands", "demo_shown",
                  "reps", "objections", "lost_reasons", "mrr_movement", "industries", "web")
    brief_bytes = len(json.dumps({k: layer[k] for k in brief_keys}).encode())

    size = out.stat().st_size / 1024
    print(f"dashboard  -> {out}  ({size:,.0f} KB)")
    print(f"  sections: overview · channels · sales · revenue · accounts")
    print(f"  accounts: {len(layer['accounts']['rows'])} rows, {len(layer['detail'])} with a timeline")
    print(f"  AI brief source: ~{brief_bytes / 1024:,.1f} KB of aggregates "
          f"(prompt ceiling {MAX_PROMPT_BYTES / 1024:,.0f} KB)")
    if brief_bytes > MAX_PROMPT_BYTES * 0.7:
        print("  WARNING: aggregates are close to the prompt ceiling — trim the brief")
    if size > 16 * 1024:
        raise SystemExit("page exceeds the 16 MB artifact limit")


if __name__ == "__main__":
    main()
