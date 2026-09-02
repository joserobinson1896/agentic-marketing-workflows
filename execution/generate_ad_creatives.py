#!/usr/bin/env python3
"""
Ad Creator — generates Unified Dashboards paid-social ad creatives from a
list of (template, niche, headline, ...) rows, plus a reviewable HTML
gallery with shortlist checkmarks.

See directives/ad_creator.md for the brand system and SOP this implements.

Usage:
    python execution/generate_ad_creatives.py --input execution/sample_variables.json --out .tmp/generated_ads/demo
    python execution/generate_ad_creatives.py --input .tmp/ad_batches/agencies.json --out .tmp/generated_ads/agencies --seed 42

    # Generate for a different product by pointing at its brand config
    # (defaults to execution/brands/unified_dashboards.json if --brand is omitted):
    python execution/generate_ad_creatives.py --input .tmp/ad_batches/leadforge.json \\
        --out .tmp/generated_ads/leadforge --brand execution/brands/leadforge.json

Brand config schema (JSON, see execution/brands/*.json):
    {"name": "...", "mark_word_1": "...", "mark_word_2": "...", "mark_svg": "<svg>...</svg>",
     "ink": "#101A17", "shadow_ink": "#123B33", "palette": {"teal": "#0EA57D", ...},
     "default_ctas": ["..."], "default_links": ["..."], "eyebrow": "...",
     "storage_key": "...", "footer_html": "..."}
    mark_svg should use the literal string INK wherever the brand ink color belongs
    (it gets substituted at render time) so the mark recolors if the brand changes.

Input schema (JSON list of row objects):
    Template A (feature benefit):
        {"template": "A", "niche": "SaaS founders",
         "headline": "MRR, churn, and CAC — finally in one place",
         "cta": "See a sample dashboard →"}          # cta optional, auto-rotated if omitted

    Template B (customer proof):
        {"template": "B", "niche": "E-commerce", "company": "Bloom Skincare",
         "headline": "Bloom Skincare cut weekly reporting from 9 hours to 40 minutes",
         "stat_label": "Weekly reporting time", "stat_value": "9h → 40min",
         "trend": "down",                              # "down" or "up" sparkline
         "cta": "See how →"}

    Template C (thought leadership):
        {"template": "C", "headline": "The real cost of checking six dashboards a day",
         "link_text": "See the breakdown →"}
"""

import argparse
import json
import random
from pathlib import Path

PALETTE = {
    "teal": "#0EA57D", "navy": "#0B3D5C", "gold": "#F2B705", "amber": "#FFC94A",
    "coral": "#FF5A36", "brick": "#8C2F1F", "plum": "#7A2E5C", "mint": "#16D9A0",
    "sky": "#2E6BC4", "cobalt": "#4457C4", "orchid": "#B23A6E", "seafoam": "#3FBF9E",
    "rose": "#E8567A", "violet": "#6B4FA0", "crimson": "#D93B56", "tangerine": "#FF8A3D",
    "slate": "#5C7AA0", "copper": "#B5652D", "emerald": "#0F8F5F", "indigo": "#3B3F8C",
}

DEFAULT_CTAS = [
    "See how →", "See a sample dashboard →", "Book a walkthrough →", "See the setup →",
    "Get your dashboard →", "See how it works →",
]
DEFAULT_LINKS = [
    "See the breakdown →", "Read the piece →", "See how we solve it →",
    "See how we think about it →", "Get the audit →",
]

INK = "#123B33"

# (x1, y1, x2, y2) gradient direction, cycled by row index % 4
PORTRAIT_DIRS = [
    (0, 0, 300, 534),
    (300, 0, 0, 534),
    (0, 534, 300, 0),
    (150, 0, 150, 534),
]
SQUARE_DIRS = [
    (0, 0, 300, 300),
    (300, 0, 0, 300),
    (0, 300, 300, 0),
    (150, 0, 150, 300),
]

# each preset: [(d, stroke_width, opacity, color), ...] — first two use white,
# the third is the ink shadow ribbon. Cycled by row index % 4, paired with the
# direction of the same index.
SHADOW_INK = "__SHADOW_INK__"  # substituted with the brand's shadow_ink at render time

PORTRAIT_PRESETS = [
    [
        ("M -40 150 C 80 90, 170 230, 340 150", 58, .48, "#fff"),
        ("M -40 270 C 110 350, 190 190, 340 300", 48, .36, "#fff"),
        ("M -40 410 C 130 460, 210 370, 340 440", 66, .26, SHADOW_INK),
    ],
    [
        ("M -40 100 C 120 200, 160 60, 340 180", 55, .45, "#fff"),
        ("M -40 250 C 100 180, 220 340, 340 260", 48, .33, "#fff"),
        ("M -40 400 C 140 350, 200 470, 340 410", 62, .24, SHADOW_INK),
    ],
    [
        ("M -40 200 C 90 260, 190 140, 340 220", 52, .43, "#fff"),
        ("M -40 340 C 120 300, 210 420, 340 350", 46, .32, "#fff"),
        ("M -40 460 C 130 490, 220 430, 340 470", 58, .24, SHADOW_INK),
    ],
    [
        ("M -40 130 C 100 220, 180 60, 340 160", 58, .46, "#fff"),
        ("M -40 300 C 130 220, 190 380, 340 290", 48, .34, "#fff"),
        ("M -40 430 C 140 470, 210 390, 340 450", 66, .26, SHADOW_INK),
    ],
]
SQUARE_PRESETS = [
    [
        ("M -40 84 C 80 50, 170 128, 340 84", 46, .46, "#fff"),
        ("M -40 151 C 110 196, 190 106, 340 168", 40, .34, "#fff"),
        ("M -40 230 C 130 258, 210 208, 340 246", 46, .2, SHADOW_INK),
    ],
    [
        ("M -40 56 C 120 112, 160 34, 340 100", 46, .46, "#fff"),
        ("M -40 140 C 100 101, 220 190, 340 146", 40, .34, "#fff"),
        ("M -40 224 C 140 196, 200 263, 340 230", 46, .2, SHADOW_INK),
    ],
    [
        ("M -40 112 C 90 146, 190 78, 340 123", 46, .46, "#fff"),
        ("M -40 190 C 120 168, 210 235, 340 196", 40, .34, "#fff"),
        ("M -40 258 C 130 274, 220 241, 340 263", 46, .2, SHADOW_INK),
    ],
    [
        ("M -40 73 C 100 123, 180 34, 340 90", 46, .46, "#fff"),
        ("M -40 168 C 130 123, 190 213, 340 163", 40, .34, "#fff"),
        ("M -40 241 C 140 263, 210 218, 340 252", 46, .2, SHADOW_INK),
    ],
]

SPARKLINES = {
    "down": "0,4 15,9 30,7 45,16 60,20",
    "up": "0,20 15,16 30,18 45,8 60,3",
}

MARK_SVG = (
    '<svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="2.6" fill="INK"/>'
    '<path d="M10 10 L2 3 M10 10 L18 2 M10 10 L17 16" stroke="INK" '
    'stroke-width="1.3" fill="none" stroke-linecap="round"/></svg>'
)
PICK_SVG = (
    '<svg viewBox="0 0 20 20"><path d="M4 10.5 8 15 16 5" fill="none" '
    'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

DEFAULT_BRAND = {
    "name": "Unified Dashboards",
    "mark_word_1": "unified",
    "mark_word_2": "Dashboards",
    "mark_svg": MARK_SVG,
    "ink": "#101A17",
    "shadow_ink": "#123B33",
    "palette": PALETTE,
    "default_ctas": DEFAULT_CTAS,
    "default_links": DEFAULT_LINKS,
    "eyebrow": "Ad Creator · generated batch",
    "storage_key": "ud-shortlist-v1",
    "footer_html": (
        "<strong>Ad Creator</strong> — generated by execution/generate_ad_creatives.py. "
        "See directives/ad_creator.md for the brand system this implements."
    ),
}


def load_brand(path):
    if not path:
        return DEFAULT_BRAND
    brand = json.loads(Path(path).read_text())
    return {**DEFAULT_BRAND, **brand}


def pick_trio(rng, used_combos, palette):
    """Pick 3 distinct colors from the brand's palette, never repeating an exact combo in this run."""
    colors = list(palette.values())
    for _ in range(200):
        trio = rng.sample(colors, 3)
        key = frozenset(trio)
        if key not in used_combos:
            used_combos.add(key)
            return trio
    # pool exhausted (very large batch) — allow a repeat rather than crash
    return rng.sample(colors, 3)


def render_bg_svg(width, height, grad_id, stops, direction, preset, shadow_ink):
    x1, y1, x2, y2 = direction
    stop_tags = "".join(
        f'<stop offset="{off}%" stop-color="{color}"/>'
        for off, color in zip((0, 50, 100), stops)
    )
    path_tags = "".join(
        f'<path d="{d}" stroke="{shadow_ink if color == SHADOW_INK else color}" stroke-width="{w}" '
        f'fill="none" opacity="{op}" stroke-linecap="round"/>'
        for d, w, op, color in preset
    )
    return (
        f'<svg class="bg" viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
        f'<defs><linearGradient id="{grad_id}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
        f'gradientUnits="userSpaceOnUse">{stop_tags}</linearGradient></defs>'
        f'<rect width="{width}" height="{height}" fill="url(#{grad_id})"/>'
        f'<g filter="url(#soft)">{path_tags}</g></svg>'
    )


def render_mark(brand):
    mark_svg = brand["mark_svg"].replace("INK", brand["ink"])
    return (
        f'<div class="mark">{mark_svg}<span class="word">{brand["mark_word_1"]}'
        f'<b>{brand["mark_word_2"]}</b></span></div>'
    )


def slugify(text):
    """Filename-safe slug, used to name the exported PNG for each ad."""
    keep = [c.lower() if c.isalnum() else "-" for c in text]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:60] or "ad"


def render_pick_button():
    return (
        '<button class="pick" type="button" aria-pressed="false" '
        f'aria-label="Shortlist this ad">{PICK_SVG}</button>'
    )


def render_slot(card_id, label, card_html):
    return (
        f'<div class="slot" data-id="{card_id}">'
        f'<div class="slot-label">{label}</div>{card_html}</div>'
    )


def render_template_a(idx, row, rng, used_combos, cta_i, brand, include_pick=True):
    stops = pick_trio(rng, used_combos, brand["palette"])
    direction = PORTRAIT_DIRS[idx % 4]
    preset = PORTRAIT_PRESETS[idx % 4]
    bg = render_bg_svg(300, 534, f"g{idx}", stops, direction, preset, brand["shadow_ink"])
    ctas = brand["default_ctas"]
    cta = row.get("cta") or ctas[cta_i % len(ctas)]
    pick = render_pick_button() if include_pick else ""
    card = (
        f'<div class="card portrait">{bg}{pick}'
        f'<div class="panel">{render_mark(brand)}<h2>{row["headline"]}</h2>'
        f'<span class="cta">{cta}</span></div></div>'
    )
    label = f'Template A · {row.get("niche", "")}'
    return {"id": f"a{idx}", "label": label, "card": card, "shape": "portrait",
            "slug": slugify(f'A-{row.get("niche", "ad")}')}


def render_template_b(idx, row, rng, used_combos, cta_i, brand, include_pick=True):
    stops = pick_trio(rng, used_combos, brand["palette"])
    direction = PORTRAIT_DIRS[idx % 4]
    preset = PORTRAIT_PRESETS[idx % 4]
    bg = render_bg_svg(300, 534, f"g{idx}", stops, direction, preset, brand["shadow_ink"])
    ctas = brand["default_ctas"]
    cta = row.get("cta") or ctas[cta_i % len(ctas)]
    trend = row.get("trend", "up")
    points = SPARKLINES.get(trend, SPARKLINES["up"])
    stroke = "#FF5A36" if trend == "down" else "#0EA57D"
    statchip = (
        '<div class="statchip">'
        f'<svg viewBox="0 0 60 24"><polyline points="{points}" fill="none" '
        f'stroke="{stroke}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        f'<div class="statchip-text"><span class="statchip-label">{row["stat_label"]}</span>'
        f'<span class="statchip-value">{row["stat_value"]}</span></div></div>'
    )
    pick = render_pick_button() if include_pick else ""
    card = (
        f'<div class="card portrait">{bg}{pick}'
        f'<div class="panel">{render_mark(brand)}<h2>{row["headline"]}</h2>'
        f'{statchip}<span class="cta">{cta}</span></div></div>'
    )
    label = f'Template B · {row.get("niche", row.get("company", ""))}'
    return {"id": f"b{idx}", "label": label, "card": card, "shape": "portrait",
            "slug": slugify(f'B-{row.get("company", row.get("niche", "ad"))}')}


def render_template_c(idx, row, rng, used_combos, link_i, brand, include_pick=True):
    stops = pick_trio(rng, used_combos, brand["palette"])
    direction = SQUARE_DIRS[idx % 4]
    preset = SQUARE_PRESETS[idx % 4]
    bg = render_bg_svg(300, 300, f"g{idx}", stops, direction, preset, brand["shadow_ink"])
    links = brand["default_links"]
    link_text = row.get("link_text") or links[link_i % len(links)]
    pick = render_pick_button() if include_pick else ""
    card = (
        f'<div class="card square">{bg}{pick}'
        f'<div class="panel">{render_mark(brand)}<h2>{row["headline"]}</h2>'
        f'<span class="link">{link_text}</span></div></div>'
    )
    label = "Template C · Thought leadership"
    return {"id": f"c{idx}", "label": label, "card": card, "shape": "square",
            "slug": slugify(f'C-{row["headline"][:40]}')}


CSS = """
:root{ --page-bg:#F5F4F0; --page-ink:#16201C; --page-muted:#5B6B63; --page-border:#DDD8CC;
  --panel-bg:#FFFFFF; --accent:#FF5A36; --pick-ring:#F2B705; }
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --page-bg:#10160F; --page-ink:#F1EFE9; --page-muted:#9CAAA1; --page-border:#2B332B; } }
:root[data-theme="dark"]{ --page-bg:#10160F; --page-ink:#F1EFE9; --page-muted:#9CAAA1; --page-border:#2B332B; }
*{box-sizing:border-box;}
body{ margin:0; background:var(--page-bg); color:var(--page-ink);
  font-family:'IBM Plex Sans', system-ui, sans-serif; padding:56px 32px 90px; }
.wrap{ max-width:1300px; margin:0 auto; }
header{ max-width:680px; margin-bottom:26px; }
.eyebrow{ font-family:'IBM Plex Mono', ui-monospace, monospace; font-size:12px; letter-spacing:.12em;
  text-transform:uppercase; color:var(--accent); margin:0 0 14px; }
h1{ font-family:'Fraunces', Georgia, serif; font-weight:700; font-size:clamp(28px,3.6vw,38px);
  line-height:1.1; letter-spacing:-0.01em; text-wrap:balance; margin:0 0 14px; }
header p{ font-size:15px; line-height:1.6; color:var(--page-muted); margin:0; }
.toolbar{ position:sticky; top:0; z-index:5; display:flex; align-items:center; justify-content:space-between;
  gap:16px; flex-wrap:wrap; background:var(--page-bg); border:1px solid var(--page-border); border-radius:10px;
  padding:12px 16px; margin:0 0 32px; }
.toolbar .count{ font-family:'IBM Plex Mono', monospace; font-size:12.5px; font-weight:500; color:var(--page-ink); }
.toolbar .count b{ color:var(--accent); }
.toolbar label{ display:flex; align-items:center; gap:8px; font-family:'IBM Plex Sans', sans-serif;
  font-size:13px; color:var(--page-muted); cursor:pointer; user-select:none; }
.toolbar input[type="checkbox"]{ width:15px; height:15px; accent-color:var(--accent); cursor:pointer; }
.board{ display:flex; flex-wrap:wrap; gap:32px; align-items:flex-start; }
.slot{ display:flex; flex-direction:column; gap:10px; width:270px; }
.slot-label{ font-family:'IBM Plex Mono', monospace; font-size:11px; letter-spacing:.02em; color:var(--page-muted); }
.slot.is-picked .slot-label{ color:var(--page-ink); font-weight:500; }
/* 270 x 480 and 270 x 270 are exactly 9:16 and 1:1 — the same ratios the PNG
   export ships at (1080x1920 / 1080x1080), so the review preview and the
   delivered asset are the same shape. */
.card{ position:relative; width:270px; border-radius:6px; overflow:hidden; border:1px solid var(--page-border);
  box-shadow:0 16px 34px -20px rgba(20,26,20,.35); font-family:'IBM Plex Sans', sans-serif;
  transition:box-shadow .15s ease; }
.card.portrait{ height:480px; }
.card.square{ height:270px; }
.slot.is-picked .card{ box-shadow:0 0 0 3px var(--pick-ring), 0 16px 34px -20px rgba(20,26,20,.4); }
.card svg.bg{ position:absolute; inset:0; width:100%; height:100%; display:block; }
.pick{ position:absolute; top:10px; right:10px; z-index:2; width:26px; height:26px; border-radius:999px;
  display:flex; align-items:center; justify-content:center; background:rgba(16,26,23,.32);
  border:1.5px solid rgba(255,255,255,.7); cursor:pointer; padding:0;
  transition:background .12s ease, border-color .12s ease; }
.pick svg{ width:13px; height:13px; }
.pick path{ stroke:#fff; opacity:.8; }
.pick.is-picked{ background:#101A17; border-color:#101A17; }
.pick.is-picked path{ opacity:1; }
.mark{ display:flex; align-items:center; gap:6px; }
.mark svg{ width:15px; height:15px; flex:none; }
.mark .word{ font-family:'IBM Plex Sans', sans-serif; font-weight:700; font-size:12px; letter-spacing:-0.01em;
  color:#101A17; line-height:1.1; }
.mark .word b{ font-weight:700; color:#101A17; letter-spacing:.09em; font-size:7px; text-transform:uppercase;
  display:block; margin-top:1px; }
.panel{ position:absolute; top:50%; left:14px; right:14px; transform:translateY(-50%); background:var(--panel-bg);
  border-radius:9px; padding:14px 14px 16px; box-shadow:0 10px 22px -12px rgba(16,26,23,.4); }
.panel h2{ font-family:'Fraunces', serif; font-weight:700; font-size:16.5px; line-height:1.18; letter-spacing:-.01em;
  margin:11px 0 11px; color:#101A17; text-wrap:balance; }
.card.square .panel{ padding:14px; }
.card.square .panel h2{ font-size:19px; line-height:1.15; margin:10px 0 12px; }
.cta{ display:inline-flex; align-items:center; gap:6px; background:#101A17; color:#fff; font-size:10.5px;
  font-weight:600; padding:8px 13px; border-radius:999px; letter-spacing:.01em; }
.link{ font-family:'IBM Plex Sans', sans-serif; font-weight:600; font-size:11.5px; color:#101A17;
  display:inline-flex; align-items:center; gap:5px; border-bottom:1.5px solid #101A17; padding-bottom:1px;
  width:fit-content; }
.statchip{ display:flex; align-items:center; gap:9px; background:#EEF6F1; border-radius:8px; padding:8px 10px;
  margin:2px 0 13px; }
.statchip svg{ width:40px; height:18px; flex:none; }
.statchip-text{ display:flex; flex-direction:column; gap:1px; }
.statchip-label{ font-family:'IBM Plex Mono', monospace; font-size:7.5px; letter-spacing:.03em;
  text-transform:uppercase; color:#5B6B63; }
.statchip-value{ font-family:'Fraunces', serif; font-weight:700; font-size:13.5px; color:#101A17; }
footer{ max-width:680px; margin-top:56px; padding-top:24px; border-top:1px solid var(--page-border);
  font-size:13px; line-height:1.6; color:var(--page-muted); }
footer strong{ color:var(--page-ink); }
"""

SCRIPT = """
(function(){
  var KEY = 'STORAGE_KEY';
  var picks;
  try{ picks = new Set(JSON.parse(localStorage.getItem(KEY) || '[]')); }
  catch(e){ picks = new Set(); }
  function save(){ try{ localStorage.setItem(KEY, JSON.stringify(Array.from(picks))); }catch(e){} }
  function updateCount(){
    document.getElementById('pick-count').innerHTML = '<b>' + picks.size + '</b> of TOTAL shortlisted';
  }
  function applyState(slot){
    var id = slot.getAttribute('data-id');
    var btn = slot.querySelector('.pick');
    var on = picks.has(id);
    btn.classList.toggle('is-picked', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    slot.classList.toggle('is-picked', on);
  }
  var slots = document.querySelectorAll('.slot');
  slots.forEach(function(slot){
    applyState(slot);
    slot.querySelector('.pick').addEventListener('click', function(){
      var id = slot.getAttribute('data-id');
      if(picks.has(id)){ picks.delete(id); } else { picks.add(id); }
      save(); applyState(slot); updateCount();
    });
  });
  updateCount();
  var filterEl = document.getElementById('filter-picked');
  filterEl.addEventListener('change', function(){
    slots.forEach(function(slot){
      var id = slot.getAttribute('data-id');
      slot.hidden = filterEl.checked && !picks.has(id);
    });
  });
})();
"""


def build_cards(rows, seed=None, brand=None, include_pick=True):
    """Build every card once, in order.

    Both the review gallery and the PNG export go through here, so a given
    (rows, seed, brand) always yields byte-identical card markup and the same
    color trios — the exported asset is exactly the ad that was reviewed.
    """
    brand = brand or DEFAULT_BRAND
    rng = random.Random(seed)
    used_combos = set()
    cards = []
    cta_i = link_i = 0
    for idx, row in enumerate(rows):
        template = row["template"].upper()
        if template == "A":
            cards.append(render_template_a(idx, row, rng, used_combos, cta_i, brand, include_pick))
            if not row.get("cta"):
                cta_i += 1
        elif template == "B":
            cards.append(render_template_b(idx, row, rng, used_combos, cta_i, brand, include_pick))
            if not row.get("cta"):
                cta_i += 1
        elif template == "C":
            cards.append(render_template_c(idx, row, rng, used_combos, link_i, brand, include_pick))
            if not row.get("link_text"):
                link_i += 1
        else:
            raise ValueError(f"Unknown template '{row['template']}' in row {idx}")
    return cards


def build_gallery(rows, seed=None, brand=None):
    brand = brand or DEFAULT_BRAND
    built = build_cards(rows, seed=seed, brand=brand, include_pick=True)
    cards = [render_slot(c["id"], c["label"], c["card"]) for c in built]

    total = len(rows)
    script = SCRIPT.replace("TOTAL", str(total)).replace("STORAGE_KEY", brand["storage_key"])
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{brand["name"]} Ad Concepts</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600..900&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}</style></head>
<body>
<svg width="0" height="0" style="position:absolute"><defs>
<filter id="soft" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="15"/></filter>
</defs></svg>
<div class="wrap">
  <header>
    <p class="eyebrow">{brand["eyebrow"]}</p>
    <h1>{brand["name"]} — {total} for review</h1>
    <p>Click the check on any card to shortlist it. Picks are saved in this browser only.</p>
  </header>
  <div class="toolbar">
    <span class="count" id="pick-count">0 of {total} shortlisted</span>
    <label><input type="checkbox" id="filter-picked"> Show shortlisted only</label>
  </div>
  <div class="board" id="board">
    {"".join(cards)}
  </div>
  <footer>{brand["footer_html"]}</footer>
</div>
<script>{script}</script>
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="Generate paid-social ad creatives for a brand.")
    parser.add_argument("--input", required=True, help="Path to a JSON file of ad rows (see script docstring).")
    parser.add_argument("--out", required=True, help="Output directory for gallery.html and individual ad files.")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducible color assignment.")
    parser.add_argument("--brand", default=None,
                         help="Path to a brand config JSON (see execution/brands/). "
                              "Defaults to the Unified Dashboards brand.")
    args = parser.parse_args()

    rows = json.loads(Path(args.input).read_text())
    if not rows:
        raise SystemExit("Input file has no rows.")

    brand = load_brand(args.brand)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    gallery_html = build_gallery(rows, seed=args.seed, brand=brand)
    (out_dir / "gallery.html").write_text(gallery_html)

    print(f"Wrote {len(rows)} ads to {out_dir / 'gallery.html'}")


if __name__ == "__main__":
    main()
