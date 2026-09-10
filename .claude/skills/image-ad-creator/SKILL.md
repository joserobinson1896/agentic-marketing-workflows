---
name: image-ad-creator
description: Generate photorealistic paid-social ad creatives for a real physical product by steering Gemini's image generation with a product photo plus style reference images. Use when the user wants product ads, photo ads, image ads, lifestyle/product photography ads, ads generated from a product picture, or asks to run the image ad creator / Gemini ad pipeline. For brand/offer ads built from HTML templates (Unified Dashboards, LeadForge) use the ad-creator skill instead.
---

# Image Ad Creator

Generates photographic product ads: a real product photo is placed into a generated scene styled after reference photography, with ad copy rendered into the image. Powered by Gemini image generation.

**Not the same as `ad-creator`.** That skill composites brand/offer ads from HTML templates and guarantees perfect typography for free. This one produces photorealistic product imagery and costs ~$0.039 per image. If the user has a physical product photo, use this. If they want gradient-and-panel brand ads for a service, use `ad-creator`.

## Steps

1. **Read `directives/image_ad_creator.md` first.** It carries the steering pattern, the QA rules, the cost controls and the aspect-ratio gotcha. Don't improvise around it.

2. **Get the product photo onto disk.** This is the usual blocker: **images pasted into the chat are not files** — you can see them but cannot write them to disk, so never claim to have saved one. Ask for the path, or search with `mdfind` (see the directive for the exact queries and the macOS screenshot filename gotcha). Copy the product photo and any style references into `execution/image_ad_creator/reference_images/` with descriptive names. Durable, not `.tmp/`: the daily job depends on them and `.tmp/` is disposable.

3. **Look at each reference image before using it.** Skip any whose subject matter shouldn't be reproduced into an ad, and tell the user which one you skipped and why.

4. **Write a batch spec JSON.** Schema is in `execution/image_ad_creator/generate_gemini_ad_batch.py`'s docstring; `execution/image_ad_creator/ai_runner_gemini_batch.json` is a working 10-ad example. One entry per ad: `id`, `style_ref`, `aspect_ratio`, `scene`, `headline`, `subline`, `cta`, `copy_placement`. Rotate style refs and vary scene/lighting/time-of-day across the batch, and mix placements (feed 4:5, square 1:1, vertical 9:16).

   Keep headline copy in **short, common words** — that is what renders reliably.

5. **Start with 3 for approval unless the user has already approved a larger run.** Each attempt costs money; a systematic prompt flaw discovered at ad 30 is an expensive mistake. This is enforced, not just advised: batches over 3 hit the spend gate, which prints the estimate and asks. Once the user has approved a number, pass `--yes-spend`.

   ```
   python execution/image_ad_creator/generate_gemini_ad_batch.py --spec <spec.json> --out .tmp/gemini_images/<run_name>
   ```
   ~10-15s per image. Run a batch of more than ~5 in the background and wait for the `GENERATED=` line.

6. **Proofread every image by reading it back.** Non-negotiable — roughly 1 in 10 comes back with a misspelled headline. Check headline, subline and CTA character by character.

7. **Fix only the failures:** `--only <id>`. If the *same word* fails twice, change the copy instead of retrying again (see the directive — "ANYWHERE" failed twice, a reworded headline worked first try).

8. **Deliver the PNGs with `SendUserFile`.** The PNGs are the deliverable; an ads manager can't take HTML. Report what was generated, any copy you changed and why, and the run's cost.

## When typography must be exact
Offer the hybrid path: generate imagery only (no copy in the prompt, negative space reserved), then overlay text with `execution/ad_creator/render_ads_to_png.py`. Perfect typesetting, brand fonts, and copy swaps without re-paying for images.

## Daily automation
A LaunchAgent runs `execution/image_ad_creator/run_daily_image_ads.py` at 8:00 AM every day: 5 ads picked statelessly from the 20-scene pool, uploaded to a dated Drive folder. See the directive's "Automation" section.

If the user asks to change the schedule, the count, or the scenes:
- Scenes/copy live in `execution/image_ad_creator/image_ad_scene_pool.json` — edit there, then **verify with `--dry-run`** (free) rather than a live run.
- `ADS_PER_DAY` and `STRIDE` are constants at the top of the runner. Keep `STRIDE` coprime with the pool size or the rotation collapses into a few fixed groups.
- Schedule lives in `~/Library/LaunchAgents/com.example.image-ads-daily.plist`; reload with `launchctl bootout` then `bootstrap`.

The daily folder is a **candidate set** — nobody has proofread the rendered text. Say so when reporting a run; don't imply it's ship-ready.

## Reference files
- `directives/image_ad_creator.md` — SOP + steering pattern + learned failure modes (source of truth)
- `execution/image_ad_creator/generate_gemini_ad_batch.py` — batch runner (`--only` regenerates single ads)
- `execution/image_ad_creator/gemini_image_generate.py` — single-image function + CLI, wraps the Gemini call
- `execution/image_ad_creator/run_daily_image_ads.py` — unattended 8 AM daily batch (`--dry-run`, `--date`)
- `execution/image_ad_creator/image_ad_scene_pool.json` — 20-scene rotation pool
- `execution/image_ad_creator/ai_runner_gemini_batch.json` — working 10-ad example spec
- `execution/image_ad_creator/reference_images/` — **operator-supplied**, not in the repo. Its README names the four expected filenames. Durable, not `.tmp/`, because the daily job depends on them
