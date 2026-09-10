"""
Layer 3 execution tool: run a batch of Gemini-generated ad creatives from a
JSON spec.

Each ad in the spec is one generate_content call conditioned on two images —
the product photo (fidelity anchor) and a style/composition reference — plus
an art-direction prompt assembled from the spec's scene/headline/CTA fields.

Spec schema (see .tmp/ad_batches/*.json):
    {
      "product_image": "path",           # image 1 for every ad
      "product_fidelity": "text",        # shared "reproduce this exact shoe" clause
      "ads": [
        {"id", "style_ref", "aspect_ratio", "scene",
         "headline", "subline", "cta", "copy_placement"}
      ]
    }

Usage:
    python execution/image_ad_creator/generate_gemini_ad_batch.py \
        --spec .tmp/ad_batches/ai_runner_gemini_batch.json \
        --out .tmp/gemini_images/ai_runner_batch

    # regenerate just a few ids (e.g. after a text-rendering typo):
    python execution/image_ad_creator/generate_gemini_ad_batch.py --spec ... --out ... --only 02_wet_street_night 07_flatlay_kit
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)
from gemini_image_generate import DEFAULT_MODEL, generate_image  # noqa: E402
from spend_gate import add_spend_argument, confirm_spend  # noqa: E402  (lives in shared/)

# The directive's own smoke test is "start with 3 for approval unless the user has already
# approved a larger run", so 3 is where the free pass belongs here rather than the shared
# default of 10. At ~$0.039 an image a 10-image free pass would be $0.39 of unasked spend,
# and an image batch is the one place a systematic prompt flaw gets expensive fastest.
FREE_PASS_IMAGES = 3


def build_prompt(spec: dict, ad: dict) -> str:
    return f"""Create a paid-social ad creative for a sneaker brand.

{spec['product_fidelity']}

SCENE / ART DIRECTION: {ad['scene']}

Render the product shoe(s) from IMAGE 1 in that scene as photorealistic
commercial product photography, with crisp material detail on the mesh, suede
and midsole.

AD COPY — render this text into the image, spelled exactly as written:
  Headline: "{ad['headline']}"
  Subline: "{ad['subline']}"
  Button: "{ad['cta']}"
{ad['copy_placement']}
Reserve clean, uncluttered negative space for the copy so nothing overlaps the
shoe. Typography must be sharp, correctly spelled, evenly kerned and
professionally set. No watermarks and no logos other than the shoe's own 'AI'
mark."""


def main():
    parser = argparse.ArgumentParser(description="Generate a batch of Gemini ad creatives")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument("--only", nargs="*", default=None, help="Only generate these ad ids")
    parser.add_argument("--image-size", default="1K")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    add_spend_argument(parser)
    args = parser.parse_args()

    with open(args.spec) as f:
        spec = json.load(f)

    ads = spec["ads"]
    if args.only:
        ads = [a for a in ads if a["id"] in args.only]
        missing = set(args.only) - {a["id"] for a in ads}
        if missing:
            print(f"WARNING: ids not found in spec: {', '.join(sorted(missing))}")

    # Check the reference images BEFORE the gate. They are not in the repo — the user
    # supplies them (see reference_images/README.md) — and generate_image only opens them
    # after the API client is built, so a missing file would otherwise surface as a
    # per-ad failure AFTER the operator had already approved the spend for the batch.
    missing = sorted({
        str(ROOT / path)
        for ad in ads
        for path in (spec["product_image"], ad["style_ref"])
        if not (ROOT / path).exists()
    })
    if missing:
        print("ERROR: reference image(s) not found:", flush=True)
        for path in missing:
            print(f"  {path}", flush=True)
        print("  These are supplied by you, not shipped in the repo. See "
              "execution/image_ad_creator/reference_images/README.md", flush=True)
        return 1

    # The gate sits after the spec is parsed and filtered, so `len(ads)` is the real number
    # of images this run would pay for, and before the output directory exists, so a refused
    # run leaves nothing behind.
    if not confirm_spend(calls=len(ads), model=args.model, label="generate ad images",
                         assume_yes=args.yes_spend, free_pass=FREE_PASS_IMAGES):
        return 1

    os.makedirs(args.out, exist_ok=True)
    ok, failed = [], []

    for i, ad in enumerate(ads, 1):
        out_path = os.path.join(args.out, f"{ad['id']}.png")
        print(f"[{i}/{len(ads)}] {ad['id']} ({ad['aspect_ratio']}) ...", flush=True)
        started = time.time()
        try:
            generate_image(
                prompt=build_prompt(spec, ad),
                reference_image_paths=[spec["product_image"], ad["style_ref"]],
                output_path=out_path,
                aspect_ratio=ad["aspect_ratio"],
                image_size=args.image_size,
                model=args.model,
            )
            print(f"    OK  {out_path}  ({time.time() - started:.0f}s)", flush=True)
            ok.append(ad["id"])
        except Exception as exc:  # keep going; one bad gen shouldn't kill the batch
            print(f"    FAIL {ad['id']}: {exc}", flush=True)
            failed.append(ad["id"])

    print(f"\nBATCH_DIR={args.out}")
    print(f"GENERATED={len(ok)}/{len(ads)}")
    if failed:
        print(f"FAILED_IDS={','.join(failed)}")
    return 0


if __name__ == "__main__":
    # sys.exit, not a bare main(): a refused spend gate returns 1 and a caller has to be
    # able to see that in the exit code rather than reading the log for it.
    sys.exit(main())
