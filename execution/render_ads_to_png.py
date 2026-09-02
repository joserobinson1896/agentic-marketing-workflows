#!/usr/bin/env python3
"""
Render each ad in a batch to a full-resolution PNG using headless Chromium.

The PNGs are the actual deliverable — the files you upload into Meta / LinkedIn
ads manager. gallery.html remains the *review* surface.

Why Chromium rather than drawing the ads with Pillow/SVG: the cards are already
HTML/CSS, so rendering them in a browser guarantees the exported asset is
pixel-identical to the gallery that was reviewed. A second, hand-written
renderer would be a second source of truth, and the two would drift.

Output sizes follow the directive's ad specs:
  Template A / B (portrait) -> 1080x1920  (9:16)
  Template C     (square)   -> 1080x1080  (1:1)
achieved by rendering the card at CSS 270x480 / 270x270 with a device scale
factor of 4, so text and the blurred gradient strokes rasterize crisply rather
than being upscaled.

Usage:
    python3 execution/render_ads_to_png.py --input .tmp/ad_batches/x.json \
        --out .tmp/generated_ads/x/png --brand execution/brands/leadforge.json
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "execution"))

from generate_ad_creatives import CSS, build_cards, load_brand  # noqa: E402

CARD_W = 270
PORTRAIT_H = 480
SQUARE_H = 270
SCALE = 4  # 270*4 = 1080, 480*4 = 1920

FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    "family=Fraunces:opsz,wght@9..144,600..900&family=IBM+Plex+Sans:wght@400;500;600;700"
    '&family=IBM+Plex+Mono:wght@400;500&display=swap">'
)

SOFT_FILTER = (
    '<svg width="0" height="0" style="position:absolute"><defs>'
    '<filter id="soft" x="-30%" y="-30%" width="160%" height="160%">'
    '<feGaussianBlur stdDeviation="15"/></filter></defs></svg>'
)

# The gallery styles a card as a *review* object — rounded, bordered, shadowed,
# sitting on a page background. A shipped ad creative is full-bleed, so strip
# that chrome and pin the exact export dimensions.
ASSET_CSS = """
html,body{{margin:0;padding:0;background:#fff;}}
.card{{border:none !important;border-radius:0 !important;box-shadow:none !important;
  width:{w}px !important;height:{h}px !important;}}
.pick{{display:none !important;}}
"""


def standalone_html(card_html, width, height):
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"{FONT_LINKS}<style>{CSS}{ASSET_CSS.format(w=width, h=height)}</style>"
        f"</head><body>{SOFT_FILTER}{card_html}</body></html>"
    )


def render_batch(rows, out_dir, brand=None, seed=None, prefix=""):
    """Render every row to a PNG in out_dir. Returns the list of written paths."""
    from playwright.sync_api import sync_playwright

    brand = brand or {}
    cards = build_cards(rows, seed=seed, brand=brand, include_pick=False)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for card in cards:
                height = PORTRAIT_H if card["shape"] == "portrait" else SQUARE_H
                page = browser.new_page(
                    viewport={"width": CARD_W, "height": height},
                    device_scale_factor=SCALE,
                )
                try:
                    page.set_content(
                        standalone_html(card["card"], CARD_W, height),
                        wait_until="networkidle",
                    )
                    # Webfonts must be resolved before the screenshot or the ad
                    # silently ships in a fallback face.
                    page.evaluate("() => document.fonts.ready")
                    name = f"{prefix}{card['slug']}.png"
                    path = out_dir / name
                    page.screenshot(path=str(path), type="png")
                    written.append(path)
                finally:
                    page.close()
        finally:
            browser.close()
    return written


def main():
    parser = argparse.ArgumentParser(description="Render ad rows to full-resolution PNGs.")
    parser.add_argument("--input", required=True, help="JSON file of ad rows.")
    parser.add_argument("--out", required=True, help="Directory to write PNGs into.")
    parser.add_argument("--brand", default=None, help="Brand config JSON (see execution/brands/).")
    parser.add_argument("--seed", type=int, default=None, help="Same seed as the gallery, to match colors.")
    args = parser.parse_args()

    rows = json.loads(Path(args.input).read_text())
    brand = load_brand(args.brand)
    written = render_batch(rows, args.out, brand=brand, seed=args.seed)
    for path in written:
        print(f"PNG={path}")
    print(f"Rendered {len(written)} PNGs to {args.out}")


if __name__ == "__main__":
    main()
