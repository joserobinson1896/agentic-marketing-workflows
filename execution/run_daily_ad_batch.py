#!/usr/bin/env python3
"""
Unattended daily entry point — invoked by launchd locally, or by a cloud
routine's prompt, Monday-Friday. Fully deterministic — no LLM call in this
generation path:

  1. Look up today's weekday in execution/rotation_plan.json to pick a brand.
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "execution"))

from generate_ad_creatives import build_gallery, load_brand  # noqa: E402
from google_drive_upload import upload_file  # noqa: E402

PLAN_PATH = ROOT / "execution" / "rotation_plan.json"
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
    print(f"DRIVE_FILENAME={brand['name']} Ad Batch — {date_str}.html")

    credentials_ready = (ROOT / "credentials.json").exists() and (ROOT / "token.json").exists()
    if not credentials_ready:
        log(
            "Local OAuth not configured (no credentials.json/token.json here) — "
            f"gallery saved locally only: {gallery_path}. "
            "A cloud routine should upload GALLERY_PATH via its Google-Drive MCP tool instead."
        )
        return

    drive_name = f"{brand['name']} Ad Batch — {date_str}.html"
    try:
        link = upload_file(gallery_path, drive_name, folder_id=DRIVE_FOLDER_ID)
        log(f"Uploaded to Drive: {link}")
    except Exception as e:
        log(f"Drive upload FAILED (gallery still saved locally at {gallery_path}): {e}")


if __name__ == "__main__":
    main()
