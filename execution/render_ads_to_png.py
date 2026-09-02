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
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "execution"))

from generate_ad_creatives import CSS, build_cards, load_brand  # noqa: E402

CARD_W = 270
PORTRAIT_H = 480
SQUARE_H = 270
SCALE = 4  # 270*4 = 1080, 480*4 = 1920

FONT_DIR = ROOT / "execution" / "fonts"
FONT_MANIFEST = FONT_DIR / "manifest.json"


def embedded_font_css():
    """Inline the vendored woff2 files as @font-face data URIs.

    Rendering must not depend on fonts.googleapis.com: the cloud sandbox has
    allowlisted egress, and a blocked font request fails *silently* — the ads
    would render in a fallback serif and upload looking wrong. See
    execution/fetch_fonts.py for how these files get here.
    """
    import base64

    if not FONT_MANIFEST.exists():
        raise RuntimeError(
            f"{FONT_MANIFEST} missing — run `python3 execution/fetch_fonts.py` once "
            "to vendor the brand webfonts."
        )

    faces = []
    cache = {}
    for entry in json.loads(FONT_MANIFEST.read_text()):
        path = FONT_DIR / entry["file"]
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError(f"Font file missing or empty: {path}")
        if entry["file"] not in cache:
            cache[entry["file"]] = base64.b64encode(path.read_bytes()).decode("ascii")
        faces.append(
            f"@font-face{{font-family:'{entry['family']}';"
            f"font-style:{entry['style']};font-weight:{entry['weight']};"
            f"src:url(data:font/woff2;base64,{cache[entry['file']]}) format('woff2');}}"
        )
    return "".join(faces)

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


REQUIRED_FAMILIES = ["Fraunces", "IBM Plex Sans", "IBM Plex Mono"]


def chromium_executable():
    """Locate a usable Chromium, preferring one the environment already ships.

    A cloud sandbox typically pre-installs a Chromium build and blocks
    cdn.playwright.dev, so a pip-installed playwright whose expected browser
    revision differs cannot download the one it wants and `launch()` fails.
    Pointing at the existing binary sidesteps the version handshake entirely.
    """
    explicit = os.environ.get("AD_CREATOR_CHROMIUM")
    if explicit and Path(explicit).exists():
        return explicit

    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not browsers_path or not Path(browsers_path).is_dir():
        return None  # let playwright resolve its own default (the local-dev case)

    base = Path(browsers_path)
    candidates = [base / "chromium"]
    candidates += sorted(base.glob("chromium-*/chrome-linux/chrome"), reverse=True)
    candidates += sorted(base.glob("chromium-*/chrome-linux64/chrome"), reverse=True)
    candidates += sorted(
        base.glob("chromium_headless_shell-*/chrome-linux/headless_shell"), reverse=True
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def assert_fonts_loaded(page):
    """Fail loudly if a brand face didn't actually parse.

    Deliberately not document.fonts.check(): that returns true when *no*
    @font-face matches (it happily falls through to a system font) and false
    for a declared-but-not-yet-used face, so it reports the opposite of the
    truth in both directions. Forcing a load and reading back the FontFace
    status tests the thing that matters — did this font file parse and become
    usable.
    """
    failed = page.evaluate(
        """async (families) => {
            const bad = [];
            for (const family of families) {
                try {
                    await document.fonts.load(`700 16px "${family}"`);
                } catch (e) {
                    bad.push(family + ' (load threw)');
                    continue;
                }
                let ok = false;
                for (const face of document.fonts) {
                    if (face.family.replace(/['"]/g, '') === family
                        && face.status === 'loaded') { ok = true; break; }
                }
                if (!ok) bad.push(family);
            }
            return bad;
        }""",
        REQUIRED_FAMILIES,
    )
    if failed:
        raise RuntimeError(
            f"Brand webfonts unavailable: {', '.join(failed)}. Ads would render in a "
            "fallback face — refusing to ship them. Check execution/fonts/."
        )


def standalone_html(card_html, width, height, font_css):
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<style>{font_css}{CSS}{ASSET_CSS.format(w=width, h=height)}</style>"
        f"</head><body>{SOFT_FILTER}{card_html}</body></html>"
    )


def render_batch(rows, out_dir, brand=None, seed=None, prefix=""):
    """Render every row to a PNG in out_dir. Returns the list of written paths."""
    from playwright.sync_api import sync_playwright

    brand = brand or {}
    cards = build_cards(rows, seed=seed, brand=brand, include_pick=False)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    font_css = embedded_font_css()
    written = []
    with sync_playwright() as p:
        executable = chromium_executable()
        browser = (
            p.chromium.launch(executable_path=executable)
            if executable
            else p.chromium.launch()
        )
        try:
            for card in cards:
                height = PORTRAIT_H if card["shape"] == "portrait" else SQUARE_H
                page = browser.new_page(
                    viewport={"width": CARD_W, "height": height},
                    device_scale_factor=SCALE,
                )
                try:
                    page.set_content(
                        standalone_html(card["card"], CARD_W, height, font_css),
                        wait_until="load",
                    )
                    # Webfonts must be resolved before the screenshot or the ad
                    # silently ships in a fallback face.
                    page.evaluate("() => document.fonts.ready")
                    assert_fonts_loaded(page)
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
