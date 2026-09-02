# Ad Creator

## Goal
Generate on-brand paid-social ad creatives by recombining three proven ad templates with a niche/offer/stat variable, so a large batch of ad concepts can be produced and reviewed quickly. This directive is the SOP behind the `Ad Creator` skill. The generator is brand-agnostic: `Unified Dashboards` is the original/default brand, but any product can plug into the same 3 templates via a brand config (see **Multi-brand support** below).

## Background
Unified Dashboards is a fictional, AI-built, done-for-you dashboard service that unifies scattered marketing/sales tool data (HubSpot, Mailchimp, Stripe, Shopify, ad platforms, GA, etc.) into one live KPI view, custom-configured per client's stack. It's a productized service, not a self-serve SaaS — copy should never imply self-signup ("Start now"); it implies a sales/demo motion ("See a sample dashboard", "Book a walkthrough").

The visual system was reverse-engineered from a set of real Stripe paid-social ads, then rebuilt from scratch with new copy, a new mark, and a new palette. **Never reuse Stripe's (or any real company's) logo, wordmark, or identifying visual assets.**

## Brand system (do not deviate without asking)
- **Wordmark:** "unified" (bold) + "DASHBOARDS" (bold, same ink color as "unified" — never light gray or a lighter weight) as a two-line lockup, with a small converging-lines node mark (three short lines meeting at a dot) to its left.
- **Typefaces:** Fraunces (headlines, weight 700) · IBM Plex Sans (UI text/CTA) · IBM Plex Mono (labels/captions) — all from Google Fonts.
- **Ink:** `#101A17` for text and the CTA pill fill; `#123B33` for the shadow ribbon behind each panel.
- **Gradient palette pool (20 colors):** teal `#0EA57D`, navy `#0B3D5C`, gold `#F2B705`, amber `#FFC94A`, coral `#FF5A36`, brick `#8C2F1F`, plum `#7A2E5C`, mint `#16D9A0`, sky `#2E6BC4`, cobalt `#4457C4`, orchid `#B23A6E`, seafoam `#3FBF9E`, rose `#E8567A`, violet `#6B4FA0`, crimson `#D93B56`, tangerine `#FF8A3D`, slate `#5C7AA0`, copper `#B5652D`, emerald `#0F8F5F`, indigo `#3B3F8C`. Every ad draws 3 non-repeating colors from this pool; no two ads in the same batch may share the same 3-color combination, and no single color should dominate a batch.
- **Panel rule:** the white card is always **vertically centered** in the ad — never pinned to the top with empty gradient space left underneath.

## The 3 templates
1. **Template A — Feature benefit** (portrait, 1080:1920 ratio): full-bleed gradient behind, white panel centered with mark + one-line benefit headline + pill CTA.
2. **Template B — Customer proof** (portrait, same ratio): same structure as A, but the headline is a specific (fictional) customer result — `"[Company] [did X] — [stat]"` — plus a compact stat chip (mini sparkline + label + value) between headline and CTA.
3. **Template C — Thought leadership** (square, 1:1): full-bleed gradient, smaller centered white panel, headline is a provocative one-line question or insight, CTA is a text link (not a pill) — e.g. "See the breakdown →".

## Reference examples — model every batch after these
These 6 are the shortlisted bar for tone, headline length, and visual balance. Every new batch should read like it belongs next to them:

| Template | Niche/Company | Headline | Supporting copy |
|---|---|---|---|
| A | SaaS founders | "MRR, churn, and CAC — finally in one place" | CTA: "See a sample dashboard →" |
| A | Mobile app studios | "Installs, retention, and revenue — one screen" | CTA: "See how it works →" |
| B | Bloom Skincare (e-commerce) | "Bloom Skincare cut weekly reporting from 9 hours to 40 minutes" | Stat chip: "Weekly reporting time — 9h → 40min" · CTA: "See how →" |
| B | Harlow Realty (real estate) | "Harlow Realty caught a $40,000 pipeline gap in their first week" | Stat chip: "Pipeline gap found — $40,000" · CTA: "See how →" |
| C | — | "The real cost of checking six dashboards a day" | Link: "See the breakdown →" |
| C | — | "What HubSpot, Stripe, and Mailchimp never agree on" | Link: "See how we solve it →" |

Patterns to keep:
- **Template A** headlines name 2–3 concrete metrics or nouns and end on "one place / one screen / one view."
- **Template B** headlines always lead with a specific fictional company name and a concrete, believable number.
- **Template C** headlines are a provocative one-sentence insight or question — never generic ("thought leadership" copy, not "read our blog").

## Multi-brand support
The generator takes an optional `--brand <path>` flag pointing to a JSON brand config (see `execution/brands/*.json`). Omitting it defaults to the Unified Dashboards brand above. A brand config supplies everything product-specific: `name`, `mark_word_1`/`mark_word_2` (the two-line wordmark), `mark_svg` (use the literal string `INK` wherever the brand ink color belongs so it recolors correctly), `ink`, `shadow_ink`, `palette` (a 20-color pool, same rules as above — can reuse the default pool if the product doesn't need its own), `default_ctas`, `default_links`, `eyebrow`, `storage_key` (must be unique per brand so shortlists don't collide across products saved in the same browser), and `footer_html`.

To add a new product: invent its identity (name, wordmark, mark icon, ink colors, CTA/link copy that matches its actual offer motion — e.g. a productized service still shouldn't say "Start free trial"), write it to `execution/brands/<product>.json`, then run the generator with `--brand execution/brands/<product>.json`. Never reuse a real company's logo/wordmark for the new brand either — same rule as Unified Dashboards.

### Brand: LeadForge (lead generation)
LeadForge is a fictional, AI-built, done-for-you B2B lead generation service — it finds, enriches, and qualifies leads matched to a client's ICP and delivers them straight into their CRM. Like Unified Dashboards, it's a productized service, not self-serve software: copy implies a sales/qualification motion ("See sample leads", "Book a strategy call"), never self-signup.
- **Wordmark:** "lead" (bold) + "FORGE" (bold, same ink) two-line lockup, with a small crosshair/target mark to its left (evokes precision targeting of the right leads).
- **Ink:** `#17130E` for text/CTA fill; `#5C2E12` (ember/forge tone) for the shadow ribbon.
- **Config:** `execution/brands/leadforge.json`

| Template | Niche/Company | Headline | Supporting copy |
|---|---|---|---|
| A | B2B SaaS sales teams | "Fit, intent, and verified contact info — before the first call" | CTA: "See sample leads →" |
| B | Brightline Roofing (home services) | "Brightline Roofing booked 47 qualified estimates in their first month" | Stat chip: "Qualified estimates booked — 0 → 47" |
| C | — | "Why most 'qualified' leads never take a sales call" | Link: "See the breakdown →" |

Patterns to keep for LeadForge specifically: Template A headlines name the concrete qualifiers a lead arrives with (fit, intent, contact info) rather than dashboard metrics; Template B still leads with a fictional company + a believable, specific number; Template C stays provocative about lead-gen/sales-ops pain points (wasted calls, bad CRM data, chasing dead leads).

## Inputs needed from the user
- A list of niches/ICPs to target (e.g. SaaS founders, e-commerce brands, agencies).
- Optionally: specific offers/angles, or fictional company names + stats to feature in Template B rows.
- How many total ads (default: 10 per template = 30, matching the last review round).

## Workflow
1. Check `execution/` before writing any new script — `generate_ad_creatives.py` already exists; extend it rather than rewriting from scratch.
2. Turn the user's niches/offers into a row list matching the script's input schema (see `execution/sample_variables.json` for the exact format and the 6 reference rows above already encoded).
3. Write that row list to `.tmp/ad_batches/<run_name>.json`.
4. Run: `python execution/generate_ad_creatives.py --input .tmp/ad_batches/<run_name>.json --out .tmp/generated_ads/<run_name>` — add `--brand execution/brands/<product>.json` when generating for a product other than Unified Dashboards.
5. This produces `gallery.html` (a shortlist-and-checkmark review page, same UI pattern used in the manual round) plus one standalone HTML file per ad in that folder.
6. If the user wants to review visually in-browser, publish `gallery.html` as an Artifact. Otherwise hand them the folder path directly.
7. Ask the user which ads they shortlisted (see limitation below) and treat those as the delivered set.

## Known limitation — reading shortlists back
If the gallery is published as an Artifact, its shortlist checkmarks are stored in the *viewer's browser* (`localStorage`) by default, which Claude cannot read. Either:
- Ask the user to report back which ones they picked (by the id shown on each card), or
- Wire the gallery's shortlist state to the Artifact's shared database (the `db` runtime capability) instead of `localStorage`, so picks become readable via `Artifact` → `read_db`. This is a known upgrade path, not yet built into `generate_ad_creatives.py` — do it if the user asks for cross-session/Claude-readable shortlists.

## Automation — unattended daily batch
`execution/run_daily_ad_batch.py` runs the whole pipeline with no LLM involved, Monday-Friday: it picks a brand from `execution/rotation_plan.json` by weekday, picks that brand's row-group by **today's date-ordinal modulo the group count** (deliberately stateless — no counter file, so it gives the same answer whether it's run on a machine with persistent disk or a cloud sandbox that starts from a fresh clone every time), then renders the gallery. It always prints `GALLERY_PATH=`, `BRAND_NAME=`, and `DRIVE_FILENAME=` lines so a calling agent doesn't have to guess the output path.

Two ways it's deployed:
- **Local (`launchd`)**: a LaunchAgent (`~/Library/LaunchAgents/com.marketingcoursedemo.ad-creator-daily.plist`) runs the script at 7:00 AM America/New_York, Mon-Fri. If `credentials.json`/`token.json` exist in the project root (a one-time OAuth grant via `execution/authorize_google_drive.py`), the script uploads the gallery to Drive itself through `execution/google_drive_upload.py`. Logs: `.tmp/daily_ad_batch.log`, `.tmp/launchd_std{out,err}.log`.
- **Cloud (RemoteTrigger routine)**: same script, run from a fresh git clone by a scheduled cloud Claude Code session. Since `credentials.json`/`token.json` are gitignored (never pushed), the script just generates and logs "saved locally only" inside the sandbox — the routine's prompt then has the agent read the `GALLERY_PATH=` line and upload that file itself via the **Google-Drive MCP connector** (`mcp__Google-Drive__*` tools), naming it with the `DRIVE_FILENAME=` value. This avoids needing OAuth secrets inside the cloud environment at all.

Only one of these two should be enabled at a time — running both would double-post the same day's batch to Drive (though never a *duplicate rotation slice*, since the date-based picker is deterministic either way). As of the cloud cutover the local LaunchAgent is unloaded (`launchctl bootout`), leaving the cloud routine as the only live schedule.

Learned while wiring up the cloud routine:
- **The cloud sandbox has no Google API client libraries.** `google_drive_upload.py` must therefore be imported *lazily*, inside the branch that actually uses it — a module-level import crashed the entire run before a single ad was generated. The cloud path never needs those libs at all; it delivers through MCP.
- **The Drive MCP tool is `mcp__Google-Drive__create_file`** (`textContent`, `contentMimeType`, `disableConversionToGoogleType: true` to keep it as a real `.html` file rather than converting to a Google Doc).
- **Uploading via MCP costs a full re-emission of the gallery HTML** as a tool argument — roughly 45s for a 3-ad, ~12KB gallery. This scales badly: a 30-ad batch would be several times that and risks truncation. If daily batches ever grow past ~10 ads, switch the cloud path to a service-account upload inside the Python script (env-var credentials) instead of routing bytes back through the model.
- **Cron is UTC-only and not DST-aware.** `0 11 * * 1-5` is 7:00 AM Eastern only while EDT is in effect; it must be changed to `0 12 * * 1-5` when EST begins, or the job drifts to 6:00 AM. There is also a few minutes of scheduler jitter, so it fires ~7:08 AM rather than exactly 7:00.
- **The sandbox clock is UTC**, so `dt.date.today()` there is the UTC date. At the 11:00 UTC run time the UTC and Eastern dates agree, so weekday/rotation selection is correct — but a manual test run late in the US evening will pick the *next* day's slice.

## Edge cases / things learned
- Don't let Template C's white panel stretch full-height — size it to its content (position with `top` + `left` + `right` only, never `bottom`), or the headline reads too small relative to the card.
- Rotate CTA/link copy across a batch — don't repeat the same string on every card. Pull from: "See how →", "See a sample dashboard →", "Book a walkthrough →", "See the setup →", "Get your dashboard →", "See how it works →", "Read the piece →", "Get the audit →".
- Template B rows need a fictional company name every time — never leave it as a generic "a customer."
- Color assignment in the script is randomized but de-duplicated per run (no repeat 3-color combos within a batch); pass `--seed` for a reproducible batch.
