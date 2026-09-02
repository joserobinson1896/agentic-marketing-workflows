---
name: ad-creator
description: Generate a batch of on-brand Unified Dashboards paid-social ad creatives (3 templates x niche/offer/stat variables) and produce a reviewable HTML gallery with shortlist checkmarks. Use when the user asks to generate ads, create ad creatives, make a new batch of Unified Dashboards ads, or run the ad creator / creative pipeline.
---

# Ad Creator

Generates Unified Dashboards paid-social ad creatives by combining 3 fixed templates with niche/offer/stat variables, then hands back a reviewable gallery.

## Steps

1. **Read `directives/ad_creator.md` first.** It has the full brand system (mark, fonts, palette, panel rules), the 3 template specs, and the 6 reference examples every batch must be styled after. Do not improvise copy or visuals that contradict it — if something about the brand system needs to change, update the directive (don't just diverge silently).

2. **Get the batch inputs from the user** if not already given:
   - Which niches/ICPs to target (default split: 10 per template = 30 total, matching prior rounds).
   - For Template B rows specifically: a fictional company name + a believable stat per niche (never leave these generic).
   - Any specific offers/angles they want represented.

3. **Build the input JSON.** Turn the gathered niches/offers into row objects matching the schema documented in `execution/generate_ad_creatives.py`'s docstring (also see `execution/sample_variables.json` for a working example — it already encodes the 6 shortlisted reference ads). Write it to `.tmp/ad_batches/<run_name>.json`.

4. **Run the script:**
   ```
   python execution/generate_ad_creatives.py --input .tmp/ad_batches/<run_name>.json --out .tmp/generated_ads/<run_name>
   ```
   This writes `.tmp/generated_ads/<run_name>/gallery.html` — a self-contained HTML document (full `<html>/<head>/<body>`) with all the ad cards plus the shortlist-checkmark review UI.

5. **Hand it back to the user.** Two options:
   - **Publish as an Artifact** so they can review and shortlist in-browser. The script's output is a *complete* HTML document — the Artifact tool wraps its own `<!doctype>/<html>/<head>/<body>` around whatever file you give it, so **strip the outer `<html>`, `<head>`, and `<body>` tags first** (keep the `<title>`, the font `<link>` tags, the `<style>` block, and everything that was inside `<body>`) before calling the Artifact tool, or the page will double-wrap and likely break. A quick way: extract the `<style>...</style>` contents and the `<body>...</body>` contents, concatenate with a `<title>` and the font `<link>` tags, and write that as the artifact source file.
   - **Just hand them the folder path** if they're reviewing locally — the file opens directly in any browser as-is, no stripping needed for that case.

6. **Collect the shortlist.** The gallery's checkmarks save to the *viewer's browser* `localStorage`, which is invisible to Claude. Ask the user which ones they picked (by the `data-id` shown in each card's slot label / by describing them). If the user wants Claude to be able to read picks back directly in the future, that requires wiring the gallery to the Artifact's shared database (the `db` capability) instead of `localStorage` — flag this as a possible upgrade, but don't build it unless asked.

## Reference files
- `directives/ad_creator.md` — brand system + SOP (source of truth)
- `execution/generate_ad_creatives.py` — the generator (extend this rather than writing a new script)
- `execution/sample_variables.json` — working example input, encodes the 6 shortlisted reference ads
