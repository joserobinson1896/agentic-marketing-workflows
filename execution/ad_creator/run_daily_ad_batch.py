#!/usr/bin/env python3
"""
Unattended daily entry point — invoked by launchd locally, or by a cloud
routine's prompt, Monday-Friday. Fully deterministic — no LLM call in this
generation path:

  1. Look up today's weekday in execution/ad_creator/rotation_plan.json to pick a brand.
  2. Pick that brand's row-group by today's date modulo the group count —
     stateless on purpose: a cloud routine gets a fresh git clone every run,
     so there's no local disk to persist a counter on. Date math needs no
     state at all and gives the same answer no matter where/how it runs.
  3. Render the gallery with generate_ad_creatives.build_gallery().
  4. Upload gallery.html to Google Drive via google_drive_upload.py, if
     credentials.json/token.json are present (the local-machine OAuth path;
     never committed to git). On a cloud routine those won't exist — the
     script just prints GALLERY_PATH= and leaves upload to the calling
     agent, which uploads via the Google-Drive MCP connector instead.

See directives/ad_creator.md ("Automation") for both delivery paths.
"""

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)

# Data that belongs to this area lives beside it.
HERE = Path(__file__).resolve().parent

from generate_ad_creatives import build_gallery, load_brand  # noqa: E402

# google_drive_upload is imported lazily inside main(), not here: it pulls in the
# Google API client libraries, which only the local-machine OAuth path needs. A
# cloud routine's sandbox has no reason to have them installed (it delivers via
# the Google-Drive MCP connector instead), and a module-level import would crash
# the whole run before a single ad got generated.

PLAN_PATH = HERE / "rotation_plan.json"
OUT_ROOT = ROOT / ".tmp" / "generated_ads"
LOG_PATH = ROOT / ".tmp" / "daily_ad_batch.log"

DRIVE_FOLDER_ID = None  # set to a Drive folder ID to file uploads there instead of "My Drive" root


def log(msg):
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def pick_group(plan, brand_key, today):
    groups = plan["brands"][brand_key]["groups"]
    idx = today.toordinal() % len(groups)
    return groups[idx]


def main():
    today = dt.date.today()
    today_name = today.strftime("%A")
    plan = json.loads(PLAN_PATH.read_text())

    brand_key = plan["weekday_brand"].get(today_name)
    if not brand_key:
        log(f"No brand mapped for {today_name} (weekend?) — nothing to do.")
        return

    rows = pick_group(plan, brand_key, today)

    brand_config_path = ROOT / plan["brands"][brand_key]["brand_config"]
    brand = load_brand(str(brand_config_path))

    date_str = dt.date.today().isoformat()
    out_dir = OUT_ROOT / f"auto_{date_str}_{brand_key}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "input_rows.json").write_text(json.dumps(rows, indent=2))

    seed = int(date_str.replace("-", ""))
    gallery_html = build_gallery(rows, seed=seed, brand=brand)
    gallery_path = out_dir / "gallery.html"
    gallery_path.write_text(gallery_html)
    log(f"Generated {len(rows)} ads for {brand['name']} ({today_name}) -> {gallery_path}")

    print(f"GALLERY_PATH={gallery_path}")
    print(f"BRAND_NAME={brand['name']}")

    # The PNGs are the actual deliverable — 1080x1920 / 1080x1080 files you can
    # upload straight into an ads manager. gallery.html stays the review surface.
    png_dir = out_dir / "png"
    try:
        from render_ads_to_png import render_batch

        pngs = render_batch(rows, png_dir, brand=brand, seed=seed)
        log(f"Rendered {len(pngs)} PNGs -> {png_dir}")
        for path in pngs:
            print(f"PNG={path}")
    except Exception as e:
        log(f"PNG rendering FAILED: {e}")
        pngs = []

    folder_name = f"{brand['name']} Ad Batch — {date_str}"
    print(f"DRIVE_FOLDER_NAME={folder_name}")

    if not pngs:
        log("No PNGs to deliver — stopping before upload.")
        return

    # Binary assets are far too large to hand back through an MCP tool call as
    # base64 (each PNG is 150-750KB), so the upload always goes straight to the
    # Drive API from here, using whatever credentials the environment provides.
    try:
        from google_drive_upload import upload_batch

        to_upload = list(pngs) + [gallery_path]
        folder_link, links = upload_batch(
            to_upload, folder_name, parent_id=DRIVE_FOLDER_ID
        )
        log(f"Uploaded {len(links)} files to Drive folder: {folder_link}")
        print(f"DRIVE_FOLDER_LINK={folder_link}")
    except Exception as e:
        log(
            f"Drive upload FAILED (assets still on disk at {png_dir}): {e}"
        )


if __name__ == "__main__":
    main()
