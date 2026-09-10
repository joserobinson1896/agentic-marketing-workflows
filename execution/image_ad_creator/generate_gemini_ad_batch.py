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
import _paths  # noqa: F401,E402  (puts every execution area on sys.path)
from gemini_image_generate import DEFAULT_MODEL, generate_image  # noqa: E402


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
    args = parser.parse_args()

    with open(args.spec) as f:
        spec = json.load(f)

    ads = spec["ads"]
    if args.only:
        ads = [a for a in ads if a["id"] in args.only]
        missing = set(args.only) - {a["id"] for a in ads}
        if missing:
            print(f"WARNING: ids not found in spec: {', '.join(sorted(missing))}")

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


if __name__ == "__main__":
    main()
