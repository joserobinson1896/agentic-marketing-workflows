#!/usr/bin/env python3
"""
Unattended daily entry point for Gemini product-image ads — invoked by launchd
at 8:00 AM local, every day.

  1. Load the scene pool (execution/image_ad_creator/image_ad_scene_pool.json).
  2. Pick today's 5 scenes by date arithmetic — stateless on purpose, exactly
     like run_daily_ad_batch.py: no counter file to lose, and a cloud runner
     working from a fresh clone picks the same slice as this machine would.
  3. Generate the 5 images via the same batch path used interactively.
  4. Upload the PNGs to a dated Google Drive folder, if credentials allow.

Costs real money (~$0.039 per image, ~$0.20 per run). Use --dry-run to see
which scenes today would pick without calling the API at all.

See directives/image_ad_creator.md ("Automation") for the full picture.
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)

# Data that belongs to this area lives beside it.
HERE = Path(__file__).resolve().parent

from gemini_image_generate import generate_image  # noqa: E402
from generate_gemini_ad_batch import build_prompt  # noqa: E402

# google_drive_upload is imported lazily inside deliver(), never at module level:
# it pulls in the Google API client libraries, which a cloud sandbox has no
# reason to have installed. A module-level import would crash the whole run
# before a single ad got generated — the same lesson as run_daily_ad_batch.py.

POOL_PATH = HERE / "image_ad_scene_pool.json"
OUT_ROOT = ROOT / ".tmp" / "gemini_images"
LOG_PATH = ROOT / ".tmp" / "daily_image_ads.log"

ADS_PER_DAY = 5
# Coprime with the 20-scene pool, so the 5-scene window lands on a different
# combination each day and only repeats a given set every 20 days. A stride
# that divides the pool size (e.g. 5) would collapse into 4 fixed groups.
STRIDE = 7

DRIVE_FOLDER_ID = None  # set to a Drive folder ID to file uploads there instead of "My Drive" root


def log(msg):
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def pick_scenes(pool, today, count=ADS_PER_DAY):
    """Today's slice of the pool — a wrapping window, chosen by date ordinal."""
    ads = pool["ads"]
    start = (today.toordinal() * STRIDE) % len(ads)
    return [ads[(start + i) % len(ads)] for i in range(count)]


def deliver(paths, folder_name):
    try:
        from google_drive_upload import upload_batch

        folder_link, links = upload_batch(paths, folder_name, parent_id=DRIVE_FOLDER_ID)
        log(f"Uploaded {len(links)} files to Drive folder: {folder_link}")
        print(f"DRIVE_FOLDER_LINK={folder_link}")
    except Exception as e:
        log(f"Drive upload FAILED (assets still on disk): {e}")


def main():
    parser = argparse.ArgumentParser(description="Daily Gemini product-image ad batch")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print today's scene selection and exit. No API calls, no cost.")
    parser.add_argument("--date", help="Simulate a date (YYYY-MM-DD) for rotation testing")
    parser.add_argument("--image-size", default="1K")
    args = parser.parse_args()

    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    pool = json.loads(POOL_PATH.read_text())
    scenes = pick_scenes(pool, today)

    if args.dry_run:
        print(f"DATE={today.isoformat()}  (pool={len(pool['ads'])}, picking {len(scenes)})")
        for s in scenes:
            print(f"  {s['id']:<24} {s['aspect_ratio']:<5} \"{s['headline']}\"")
        print("DRY_RUN=1  (no images generated, no cost incurred)")
        return

    date_str = today.isoformat()
    out_dir = OUT_ROOT / f"auto_{date_str}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "selected_scenes.json").write_text(json.dumps(scenes, indent=2))

    log(f"Starting daily image-ad batch for {date_str}: {', '.join(s['id'] for s in scenes)}")

    generated = []
    for i, ad in enumerate(scenes, 1):
        out_path = out_dir / f"{ad['id']}.png"
        try:
            generate_image(
                prompt=build_prompt(pool, ad),
                reference_image_paths=[str(ROOT / pool["product_image"]),
                                       str(ROOT / ad["style_ref"])],
                output_path=str(out_path),
                aspect_ratio=ad["aspect_ratio"],
                image_size=args.image_size,
            )
            log(f"  [{i}/{len(scenes)}] OK {ad['id']}")
            generated.append(out_path)
            print(f"IMAGE={out_path}")
        except Exception as e:
            # One bad generation must not sink the batch — the rest still ship.
            log(f"  [{i}/{len(scenes)}] FAILED {ad['id']}: {e}")

    print(f"BATCH_DIR={out_dir}")
    print(f"GENERATED={len(generated)}/{len(scenes)}")

    if not generated:
        log("No images generated — stopping before upload.")
        return

    # NOTE: nothing here proofreads the rendered headline text. Roughly 1 in 10
    # generations comes back misspelled (see directives/image_ad_creator.md),
    # and only a human or an LLM can catch that — so treat this folder as a
    # daily *candidate* set to review, not as ship-ready creative.
    deliver(generated, f"AI Runner Image Ads — {date_str}")


if __name__ == "__main__":
    main()
