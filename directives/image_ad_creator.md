# Image Ad Creator (Gemini)

## Goal
Generate photorealistic paid-social ad creatives for a **real physical product** by conditioning Google's Gemini image model on a product photo plus a style/composition reference, then rendering ad copy into the image. This is the SOP behind the `Image Ad Creator` skill.

## How this differs from `ad_creator.md`
Both directives make paid-social ads; they are **not interchangeable**, and neither replaces the other.

| | `ad_creator.md` | `image_ad_creator.md` (this one) |
|---|---|---|
| Output | Brand/graphic ads — gradient + white panel + headline | Photographic product ads — the product shot in a real scene |
| Engine | Deterministic HTML/CSS composited, screenshotted by Chromium | Gemini image generation (probabilistic) |
| Needs | Copy variables only | A real product photo on disk |
| Typography | Pixel-perfect, guaranteed | Model-rendered — **must be proofread** |
| Cost | Free (local render) | ~$0.039 per image, per attempt |

Route to `ad_creator.md` when the ask is brand/offer ads for a service (Unified Dashboards, LeadForge). Route here when there is a physical product photo to place in a scene.

## Inputs needed from the user
- **A product photo as a file on disk.** Required, and the usual blocker — see "Getting the product photo" below.
- **Style/composition reference images** (optional but strongly recommended): existing ads or product photography whose lighting, camera angle and crop should be emulated.
- How many ads, and any copy/angle direction. Default: start with 3 for approval, then scale.

### Getting the product photo
**Images pasted into the chat are not files.** Claude can see them but has no tool to write those bytes to disk — Bash/Read/Write only reach the real filesystem, and pasted attachments are not saved there. Do not claim to have saved one.

To find the real file, in order:
1. Ask the user for the path, or
2. Search the filesystem — `mdfind` (Spotlight) is the fastest: `mdfind -onlyin "$HOME" "sneaker"`, or `mdfind -onlyin "$HOME" 'kMDItemContentTypeTree == "public.image" && kMDItemFSContentChangeDate >= $time.today'` to catch screenshots taken during the conversation.

Then copy everything into `execution/image_ad_creator/reference_images/` with descriptive names before generating. Not `.tmp/` — see the note under Automation on why these have to survive a fresh clone.

**macOS screenshot filenames contain a narrow no-break space (U+202F) before "AM"/"PM"**, so a copy-pasted `cp "…at 4.13.59 PM.png"` fails with "No such file or directory". Use a glob instead: `cp ~/Desktop/Screenshot\ 2026-09-02\ at\ 4.13.59*.png dest.png`.

## Content guardrail
Reference images are the user's own picks and are usually fine, but **look at each one before using it**. Do not use a reference whose subject matter would be reproduced into the ad inappropriately — e.g. a shoe photographed with baggies of pills/powder (drug paraphernalia). Say plainly which reference is being skipped and why, and generate from the remaining ones. Don't silently drop it.

## The steering pattern (the core technique)
Every generation is one `generate_content` call with **two input images plus a text prompt**, in this order:

1. **IMAGE 1 = the product.** Prefix the prompt with a fidelity clause that describes the product in concrete detail (colorway, materials, logo placement, sole, accents) and instructs: *"reproduce this exact product with total fidelity… do not redesign, recolor, or restyle."*
2. **IMAGE 2 = the style reference.** This must be labeled explicitly: *"IMAGE 2 is the STYLE/COMPOSITION REFERENCE only — borrow its camera angle, lighting and crop, **never its product**."* Without that phrasing the model blends the two products into a hybrid. This one sentence is what makes the technique work.
3. **Scene + copy direction.** Scene description, then the exact headline/subline/CTA strings, then where to place them, then a reservation of clean negative space so copy doesn't collide with the product.

Product fidelity from this pattern is genuinely high — colorway, panel breakup, logo shape and placement all survive across wildly different scenes.

## Workflow
1. Gather inputs; get the product photo onto disk (above).
2. Write a batch spec JSON (schema in `execution/image_ad_creator/generate_gemini_ad_batch.py`'s docstring; working example at `execution/image_ad_creator/ai_runner_gemini_batch.json`). One entry per ad: `id`, `style_ref`, `aspect_ratio`, `scene`, `headline`, `subline`, `cta`, `copy_placement`. Vary the scene *and* the style ref across the batch so the set doesn't look repetitive.
3. Run:
   ```
   python execution/image_ad_creator/generate_gemini_ad_batch.py --spec <spec.json> --out .tmp/gemini_images/<run_name>
   ```
   ~10-15s per image, sequential. A 10-ad batch takes ~2 minutes — run it in the background and wait on the `GENERATED=` line.
4. **QA every image by reading it** (see below). Non-negotiable.
5. Regenerate only the failures: `--only <id> <id>`.
6. Deliver the PNGs with `SendUserFile`. The PNGs are the deliverable — an ads manager can't take HTML.

## QA — always proofread the rendered text
Text rendering is the model's weak point and the single most common defect. In a 10-ad batch expect roughly 1 in 10 to come back misspelled ("ENGINEREED", "ANYWHRE"). **Read back every generated image and check the headline, subline and CTA character by character** before handing anything over. Shipping a typo'd ad is worse than shipping nine.

Learned about failure modes:
- **Short, common words render reliably; longer or less-common words are where it breaks.** Write headlines with that constraint in mind.
- **A word that fails tends to keep failing.** "GO ANYWHERE" came back "ANYWHRE", then "ANYWHIRE" on retry. After two failures on the same word, **change the copy** rather than paying for more retries — "GO YOUR OWN WAY" rendered perfectly first try.
- Small incidental text *on the product itself* (a heel tab wordmark) reliably comes out as garble. Instruct "do not add any other brand marks or small text onto the product." At ad viewing size it reads fine; at full resolution it doesn't.
- The model occasionally drops trailing punctuation. Cosmetic — not worth a retry.

### The hybrid alternative
If typography must be perfect (long headlines, legal text, exact brand fonts), generate the **imagery only** — no copy in the prompt, clean negative space reserved — and overlay text deterministically with the HTML/CSS + Chromium renderer in `execution/ad_creator/render_ads_to_png.py`. That guarantees typesetting, lets copy be swapped without re-paying for the image, and keeps brand fonts exact. Offer this whenever a batch needs more than a few short words of copy.

## Aspect ratio — outputs snap to the nearest supported bucket
`ImageConfig.aspect_ratio` officially supports `1:1, 2:3, 3:2, 3:4, 4:3, 9:16, 16:9, 21:9`. Anything else **does not error** — it silently returns the nearest supported size:

| Requested | Actually returned |
|---|---|
| `4:5` | 896x1152 (7:9) |
| `9:16` | 768x1344 (4:7) |
| `1:1` | 1024x1024 (1:1) |

So a "4:5" ad is not 1080x1350 and a "9:16" ad is not 1080x1920. For placements with exact pixel specs (Meta feed 1080x1350, Stories 1080x1920), the PNGs need a crop/resize pass before upload. Verify real dimensions with PIL rather than trusting the requested ratio.

## Cost control
- `gemini-2.5-flash-image` is roughly **$0.039 per generated image**, charged per *attempt* — retries cost the same as first tries.
- Generate review rounds at `--image-size 1K`; only re-run finals at 2K if the user needs print/high-res.
- **Always generate a small approval batch (3) before a large one.** A 30-ad batch with a systematic prompt flaw wastes ~$1.20 and the user's time.
- The `--only` flag exists so one bad ad costs $0.04 to fix, not a whole batch re-run.
- **The "3 first" rule is enforced in code, not requested in prose.**
  `generate_gemini_ad_batch.py` runs through `execution/shared/spend_gate.py` with a free pass
  of **3** rather than the shared default of 10, because 10 images is $0.39 of unasked spend
  and 3 is exactly the approval batch this section already calls for. Above 3 it prints the
  estimate and asks; run non-interactively it refuses outright rather than assuming consent.
  Pass `--yes-spend` once the user has approved the number. The gate is placed after the spec
  is parsed and filtered, so the estimate reflects the real ad count including `--only`, and
  before the output directory is created, so a refused run leaves nothing behind.

## API reference
Verified against `google-genai` 2.22.0 (installed) and the SDK source, not just docs.

```python
from google import genai
from google.genai import types
from PIL import Image

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])  # .env, loaded via python-dotenv
response = client.models.generate_content(
    model="gemini-2.5-flash-image",
    contents=[prompt, Image.open(product), Image.open(style_ref)],
    config=types.GenerateContentConfig(
        response_modalities=["TEXT", "IMAGE"],
        image_config=types.ImageConfig(aspect_ratio="4:5", image_size="1K"),
    ),
)
for part in response.candidates[0].content.parts:
    if part.inline_data is not None:
        part.as_image().save(out_path)
```

Notes:
- Dependencies: `google-genai`, `pillow`, `python-dotenv`. Installing `google-genai` upgrades `google-auth` past what `google-auth-oauthlib` 1.2.3 pins — pip warns, but the Drive upload path still works. Watch for it if Drive auth ever starts failing.
- `client.models.generate_content` is the stable path. Current docs also front a newer `client.interactions` API (real — it exists in the SDK) and newer image models (`gemini-3.1-flash-image`, `gemini-3-pro-image`, up to 4K, more reference images). Untested here; `gemini-2.5-flash-image` is what this pipeline is proven on. Pass `--model` to experiment.
- Up to 14 reference images are accepted; this pipeline uses 2.
- The SDK prints a harmless "Direct use of automatic function calling (AFC)" warning on every call — filter it from output, it is not an error.
- A generation can return text but no image (safety refusal). `generate_image()` raises with the `finish_reason` and any text; the batch runner logs it and continues.

## Automation — unattended daily batch
`execution/image_ad_creator/run_daily_image_ads.py` generates **5 ads every morning at 8:00 AM local**, with no LLM in the loop.

- **Scene rotation is stateless**, same principle as `run_daily_ad_batch.py`: today's 5 are a wrapping window into the 20-scene pool at `execution/image_ad_creator/image_ad_scene_pool.json`, offset by `date.toordinal() * 7`. The stride (7) is coprime with the pool size (20) deliberately — a stride that divides the pool would collapse the rotation into 4 fixed groups. Verified: 20 distinct daily sets, all 20 scenes used across 20 days, first exact repeat on day 21.
- **`--dry-run` prints today's selection without calling the API.** Use it for any rotation change — it costs nothing. `--date YYYY-MM-DD` simulates a future day.
- **Delivery:** uploads the PNGs to a dated Drive folder (`AI Runner Image Ads — <date>`) via `google_drive_upload.py`. A failed upload is logged, never fatal — the PNGs stay on disk at `.tmp/gemini_images/auto_<date>/`.
- **Cost: ~$0.20/day, ~$6/month.** Failures don't retry automatically, by design — an unattended retry loop on a paid API is how a bad prompt turns into a large bill.
- **The daily runner is deliberately NOT spend-gated.** It imports `generate_image` directly rather than going through the batch script's `main()`, so `execution/shared/spend_gate.py` never sees it. That is correct: setting up a daily batch IS the approval for that batch, and a gate here would refuse every night, since an unattended job is not a terminal. The gate covers ad-hoc and agent-initiated batches, which is where unapproved spend actually happens.
- Logs: `.tmp/daily_image_ads.log`, plus `.tmp/image_ads_std{out,err}.log` from launchd.

### The daily output is a candidate set, not ship-ready creative
Nothing in the unattended path proofreads rendered text, and ~1 in 10 generations misspells a headline. **Someone must review the Drive folder before anything runs as an ad.** This is the one part of the interactive workflow that cannot be automated away — treat the folder as a daily shortlist to pick from.

### Local (launchd) — currently live
`~/Library/LaunchAgents/com.example.image-ads-daily.plist`, 8:00 AM every day, loaded via `launchctl bootstrap gui/$(id -u) <plist>`. Only fires when the Mac is awake and logged in; launchd runs a missed job once at next wake.

**launchd gives the script no shell environment**, so `gemini_image_generate._get_client()` loads `.env` by absolute path rather than a cwd-relative `load_dotenv()` search — a bare search silently finds nothing and fails later at auth. Verify any change to that path with `env -i <abs python> <script> --dry-run`, which reproduces launchd's bare environment.

### Google Drive authorization (done — but the setup has two traps)
Delivery runs on an OAuth Desktop client (`credentials.json`, from your own Google Cloud project) with a single scope, `drive.file`. `execution/shared/authorize_google_drive.py` writes `token.json` (gitignored) holding the refresh token.

Two traps cost a detour the first time:
1. **A consent screen in "Testing" status refuses everyone not on its test-user list** — including the project owner — with `Error 403: access_denied`, "has not completed the Google verification process."
2. **Worse, and easy to miss: apps in Testing status with External user type have their refresh tokens revoked by Google after 7 days.** Simply adding yourself as a test user "fixes" the 403 but leaves an unattended daily job that silently dies every week.

**The fix for both is to publish the app to Production** (Google Cloud Console → APIs & Services → Audience → Publish app). This needs no verification review, because `drive.file` is classified **non-sensitive** — it only grants access to files the app itself created, never the user's existing Drive. Had the pipeline used `drive.readonly` or full `drive` (both sensitive/restricted), publishing would have required a verification process and a CASA security assessment.

Verified working: a 1.1 MB PNG uploads to a dated folder and returns a shareable link. The `google-auth` version conflict pip warns about (from installing `google-genai`) does **not** break the upload path.

### Cloud cutover (routine created, disabled pending secrets)
A cloud routine named "AI Runner Image Ads Daily", cron `0 12 * * *`, created disabled. Its
trigger id is in your own routine list; nothing here needs to hardcode it.

It stays disabled until two environment variables are set on it:
- `GEMINI_API_KEY` — a fresh clone has no `.env`, so without this every run fails at auth.
- `GOOGLE_OAUTH_TOKEN_JSON` — the contents of the local `token.json`. `google_drive_upload.get_credentials()` checks this env var before falling back to the file, so the cloud path needs no `token.json` on disk.

**Set these through the claude.ai routine UI, not from an agent session.** Passing a credential as a tool-call argument writes it into the conversation transcript, and the Claude Code auto-mode classifier blocks commands that print secrets to stdout for exactly that reason. That guardrail is correct — don't route around it.

Why the reference images live in `execution/image_ad_creator/reference_images/` (~6.2 MB) rather than `.tmp/`: a fresh clone must have them, and `.tmp/` is both gitignored and disposable. A daily job cannot depend on disposable inputs.

**The cloud routine does something launchd cannot: proofread.** Its prompt has the agent Read each generated PNG and compare the rendered text against `selected_scenes.json`, then name the defective ads in its report. The local job has no LLM in the loop and ships typos silently. That QA step is the main reason to prefer the routine.

**Only one scheduler may be live at a time**, or the same day's slice generates and uploads twice — double spend, duplicate Drive folders. Before enabling the routine, unload the local job:
`launchctl bootout gui/$(id -u)/com.example.image-ads-daily`

**DST:** `0 12 * * *` is 8:00 AM only while EDT is in effect. When EST begins it becomes 7:00 AM and must be changed to `0 13 * * *`. Cron is UTC-only and not DST-aware.

## Edge cases / things learned
- Label the style reference as "style only, never its product" — omitting this blends the two products together.
- Don't reuse one style reference for a whole batch; rotate refs *and* vary scene, lighting and time of day, or the set looks like one shoot.
- Mix placements in a batch (feed / square / vertical) so the user gets multi-placement coverage from one run.
- Reserve negative space in the prompt explicitly, or copy lands on top of the product.
