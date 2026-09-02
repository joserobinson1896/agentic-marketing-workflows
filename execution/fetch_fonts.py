#!/usr/bin/env python3
"""
One-off: download the brand webfonts into execution/fonts/ so PNG rendering
never depends on the network.

Run again only if the brand typefaces change:
    python3 execution/fetch_fonts.py

Why vendor them: the cloud sandbox that renders the daily batch has allowlisted
egress, so fonts.googleapis.com may be unreachable. A missing webfont doesn't
error — the ads just silently render in a fallback serif and get uploaded
looking wrong. Embedding the files makes rendering deterministic and offline.

Fraunces and IBM Plex are both SIL Open Font License, so vendoring is fine.
Writes the .woff2 files plus manifest.json, which render_ads_to_png.py turns
into @font-face rules with base64 data URIs.
"""

import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "execution" / "fonts"
MANIFEST = FONT_DIR / "manifest.json"

# A modern browser UA makes the Google Fonts CSS API return woff2.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Only the families/weights the ad templates actually use.
QUERIES = [
    "family=Fraunces:opsz,wght@9..144,700",
    "family=IBM+Plex+Sans:wght@400;500;600;700",
    "family=IBM+Plex+Mono:wght@400;500",
]

# The ads are English-only; other subsets would multiply the payload for glyphs
# that never get drawn.
WANTED_SUBSET = "latin"

FACE_RE = re.compile(r"/\*\s*([\w-]+)\s*\*/\s*@font-face\s*\{(.*?)\}", re.S)


def fetch(url, as_text=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req) as resp:
        data = resp.read()
    return data.decode("utf-8") if as_text else data


def field(body, name):
    match = re.search(rf"{name}:\s*([^;]+);", body)
    return match.group(1).strip().strip("'\"") if match else None


def main():
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []
    seen_urls = {}

    for query in QUERIES:
        css = fetch(f"https://fonts.googleapis.com/css2?{query}&display=swap", as_text=True)
        for subset, body in FACE_RE.findall(css):
            if subset != WANTED_SUBSET:
                continue
            url_match = re.search(r"url\((https://[^)]+\.woff2)\)", body)
            if not url_match:
                continue
            url = url_match.group(1)
            family = field(body, "font-family")
            weight = field(body, "font-weight")
            style = field(body, "font-style") or "normal"

            if url in seen_urls:
                filename = seen_urls[url]
            else:
                safe = family.replace(" ", "") if family else "font"
                filename = f"{safe}-{weight}-{style}.woff2".replace(" ", "")
                (FONT_DIR / filename).write_bytes(fetch(url))
                seen_urls[url] = filename
                size = (FONT_DIR / filename).stat().st_size / 1024
                print(f"saved {filename}  ({size:.1f} KB)")

            manifest.append(
                {"family": family, "weight": weight, "style": style, "file": filename}
            )

    MANIFEST.write_text(json.dumps(manifest, indent=2))
    total = sum(f.stat().st_size for f in FONT_DIR.glob("*.woff2")) / 1024
    print(f"\n{len(manifest)} faces, {total:.1f} KB total -> {MANIFEST}")


if __name__ == "__main__":
    main()
