"""Shared path bootstrap for every script under execution/.

Scripts are grouped into per-skill areas (execution/<area>/) but still import
each other as flat top-level modules: rb2b_site and build_leads_dashboard pull
load_brand from generate_ad_creatives, mock_visitors reuses
render_ads_to_png.chromium_executable, generate_outreach_emails borrows the
Gemini client, and three daily runners call google_drive_upload. Putting every
area on sys.path here keeps those cross-area imports working no matter which
folder a script is run from, so moving a script between areas never means
rewriting the imports that reach it.

Canonical header for a script at execution/<area>/<script>.py:

    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _paths import ROOT  # noqa: E402  (also puts every area on sys.path)

ROOT is the project root, so existing ROOT / ".tmp" / ... output paths are
unchanged. Data that belongs to one skill (mock_data/, reference_images/) sits
beside its scripts and is addressed relative to the script's own folder rather
than through ROOT; anything a second area needs (brands/, fonts/,
sales_frameworks/) has moved to shared/ and is named below.
"""

import sys
from pathlib import Path

EXECUTION = Path(__file__).resolve().parent
ROOT = EXECUTION.parent

AREAS = (
    "shared",
    "ad_creator",
    "image_ad_creator",
    "visitor_identification",
    "cold_email",
    "dashboard",
    "mock_data",
)

# Assets used by more than one area live under shared/ and are named here so no
# script has to hardcode the layout: the brand system is loaded by the ad
# generator and by the visitor site/dashboard, the vendored webfonts are used by
# the PNG renderer and the visitor site, and the client's sales playbook is the
# authority on copy for both the warm visitor emails and the cold campaign.
SHARED = EXECUTION / "shared"
BRANDS = SHARED / "brands"
FONTS = SHARED / "fonts"
SALES_FRAMEWORKS = SHARED / "sales_frameworks"
# The tech-stack vocabulary: `data_source_categories` and `bi_tools` are the
# canonical names both email areas segment on, so the file moved to shared/
# alongside enrichment_providers.py when the cold pipeline started enriching too.
TECH_STACKS = SHARED / "tech_stacks.json"

for _area in AREAS:
    _area_path = str(EXECUTION / _area)
    if _area_path not in sys.path:
        sys.path.insert(0, _area_path)
