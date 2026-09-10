# Visitor Identification

## Goal

Turn anonymous traffic on the Unified Dashboards site into named, enriched leads with a
personalized outreach email waiting for each one.

The chain: **ad click → identified person → tech stack → drafted email → review dashboard.**

Everything downstream of the webhook is production code written against RB2B's real,
documented contract. Only the identity resolution itself is mocked, because that is the one
step that requires their paid graph. See **Going live** for the cutover, which is a config
change and two deleted files.

## Background — why it is built this way

Running this for real needs three things we don't control: a live public website, real
human traffic, and a paid RB2B account. So the site is real (a Flask app you can browse),
the traffic is real (browser sessions that actually execute the pixel), the webhook is real,
and only the "who is this person" step is stand-in data.

## How this differs from the other directives

| | `ad_creator.md` | `image_ad_creator.md` | **this** |
|---|---|---|---|
| Produces | HTML/CSS ad creatives | Gemini photo ads | Identified leads + outreach emails |
| Input | Copy variables | Product + style photos | Website traffic |
| Brand | Unified Dashboards / LeadForge | AI Runner sneaker | Unified Dashboards |
| Deliverable | PNG batch in Drive | PNG batch in Drive | Review dashboard + `.eml` drafts in Drive |
| Runs ads? | Yes | Yes | **No** — it consumes ad *references*, and never invokes the creative scripts |

The mock ad references in `execution/visitor_identification/mock_data/ad_references.json` are hand-authored on
purpose. This pipeline must never call `generate_ad_creatives.py` or the Gemini batch.

## What RB2B actually does — verified against their docs

This research is the expensive part of the project. Do not re-derive it; correct it here if
RB2B changes.

| Fact | Consequence |
|---|---|
| **No pull API for visitor data.** The OEM REST API (`https://app.rb2b.com/api/v1`, header `Api-Key`) only exposes `/add_domain`, `/delete_domain`, `/domains`, `/credit_usage`. | The architecture must be a **webhook receiver**, not a poller. There is no endpoint to ask "who visited today". |
| Webhook sends **one JSON object per person**, never batched. | Receiver handles a single record per request. |
| Keys are **space-separated Title Case**: `LinkedIn URL`, `First Name`, `Last Name`, `Title`, `Company Name`, `Business Email`, `Website`, `Industry`, `Employee Count`, `Estimate Revenue`, `City`, `State`, `Zipcode`, `Seen At`, `Referrer`, `Captured URL`, `Tags`, `is_repeat_visit`. | Normalize at the boundary. `Estimate Revenue` is their wording, not our typo. |
| Only `LinkedIn URL` and `First Name` are guaranteed. `Business Email`, `Title`, `Company Name` are all nullable. | Enrichment and drafting must survive a two-field record. |
| `Employee Count` is typed **integer OR string**, and real values include bands. | Coerce defensively; keep the original. |
| **No HMAC or signature verification.** RB2B's documented auth is a random token in the URL. | `?token=…`, compared with `hmac.compare_digest`. Rotate before deploying publicly. |
| Fires on **initial visit only** by default; "Send repeat visitor data" is an opt-in toggle. | Dedupe on `LinkedIn URL` anyway — that's one dashboard setting away from duplicate outreach. |
| **RB2B disables a webhook that repeatedly times out** and emails the account owner. | The receiver only validates, normalizes and appends. All slow work runs later as a batch. Never add inline enrichment or LLM calls to the handler. |
| Pixel goes immediately before `</head>`, loading `https://ddwl4m2hdecbv.cloudfront.net/b/{ID}/{ID}.js.gz` after `reb2b.SNIPPET_VERSION` and `reb2b.load("<ID>")`. | Installing site-wide rather than homepage-only reportedly yields far more profiles. |
| RB2B resolves **US traffic only**. | Non-US visitors never produce a webhook. Plan volume accordingly. |

## Architecture

```
mock_visitors.py ──real browser sessions──> rb2b_site.py  (Flask, Unified Dashboards)
                                                  │ pixel: shim (mock) | RB2B snippet (live)
                                                  ▼
                                          /mock/collect ──> rb2b_mock_resolver.py   ⟵ ONLY MOCK
                                                             US-only · match rate · dedupe · delay
                                                                    │ RB2B-exact payload
        ═════════════ everything below is production code ═════════▼════
                                      POST /rb2b/webhook?token=…
                                                ▼
                              .tmp/visitors/<run>/identified.jsonl
                                                ▼
                      enrich_visitors.py    (tech stack + ad attribution)
                                                ▼
                  generate_outreach_emails.py   (Gemini + your sales frameworks)
                                                ▼
                     build_leads_dashboard.py ──> Drive via upload_batch()
```

## Inputs needed from the user

- How many visitors (default: 10).
- Whether to include 2 non-US sessions to demonstrate the US-only filter (`--include-international`).
- Whether emails should actually be generated (costs Gemini tokens) or `--dry-run`.
- Match rate — default `1.0` so every visitor resolves and the demo yields a full set.
  `--match-rate 0.3` behaves like production RB2B.

## Workflow

The one-command path:

```bash
python execution/visitor_identification/run_visitor_pipeline.py --run-name demo --visitors 10 --include-international --fresh
```

Every step also runs standalone:

```bash
# 1. Serve the site (foreground; leave it running)
python execution/visitor_identification/rb2b_site.py --run-name demo

# 2. Drive traffic through it (separate shell)
python execution/visitor_identification/mock_visitors.py --visitors 10 --include-international

# 3. Enrich what landed
python execution/visitor_identification/enrich_visitors.py --run-name demo

# 4. Draft the emails  (--dry-run writes prompts and spends nothing)
python execution/visitor_identification/generate_outreach_emails.py --run-name demo

# 5. Export everything to one spreadsheet-friendly CSV
python execution/visitor_identification/export_leads_csv.py --run-name demo

# 6. Build the review dashboard
python execution/visitor_identification/build_leads_dashboard.py --run-name demo

# 7. Tests — run after touching anything RB2B- or provider-shaped
python execution/visitor_identification/test_rb2b_receiver.py
python execution/visitor_identification/test_enrichment_providers.py
```

Outputs land in `.tmp/visitors/<run-name>/`:
`identified.jsonl` → `enriched.json` → `emails/*.eml` + `emails/drafts.json` → `leads.csv`
+ `leads_dashboard.html`.

`leads_dashboard.html` is the **review** surface and `leads.csv` is the same data flat, one
row per lead across 40 columns (identity → enrichment → attribution → the draft itself).
The **deliverable** is those two plus the `.eml` drafts, uploaded to a Drive folder named
`Identified Visitors — <run-name>`. The CSV is written with a UTF-8 BOM so Excel doesn't
mangle the em dashes in generated copy.

## Enrichment providers — what to plug in for production

The seam is `execution/shared/enrichment_providers.py`. Every provider implements one function:

    fetch(domain, record) -> [{"tool", "category", "confidence", "source"}, ...]

Nothing downstream reads a provider's raw response, so going live is one function plus a
category mapping — no changes to `enrich_visitors.py`, the drafting or the dashboard.

| Provider | Endpoint / auth | Tech stack? | Notes |
|---|---|---|---|
| `mock` | none | fixtures | The only one that runs without credentials |
| `apollo` | `GET api.apollo.io/api/v1/organizations/enrich`, header `x-api-key` | **Yes** — `current_technologies` `[{uid, name, category}]` plus `technology_names` | **Best starting point.** Ships an explicit per-tool category, which is exactly our contract, and returns firmographics in the same call |
| `zoominfo` | Enrich Company API, JWT auth (not a static key) | **Yes** — 30k+ technologies tracked | Deepest coverage; enterprise contract, and the JWT exchange needs a token cache |
| `builtwith` | `GET api.builtwith.com/v21/api.json?KEY=&LOOKUP=` | **Yes** — detected from the live site | Best signal for what is *actually on their pages now*; cheapest to start |
| `clearbit` | `GET company.clearbit.com/v2/companies/find`, Bearer | Partial — `tech` slugs, no per-tool category | Now HubSpot Breeze Intelligence; categories get inferred from tool names |
| `salesnav` | — | **No** | Registered only to explain itself, see below |

**LinkedIn Sales Navigator cannot do this.** It's the one people ask for, because Sales Nav
shows tech-stack filters in its UI. There is no API behind them: LinkedIn's SNAP partner
programme is closed to new partners (no form, no waitlist, no published timeline), and even
existing partners don't get technographics exposed to third parties. Sales Nav stays a
manual research tool. Use `apollo` or `builtwith` for the stack.

`normalize_stack()` is the part that actually matters and is fully tested offline: every
vendor invents its own category vocabulary (`marketing_automation`, `Business Intelligence`,
`Payment Processors`, or bare slugs with no category at all) and our `siloed_sources` count
— which the whole pitch is built from — keys off canonical names. Unmapped vendor categories
fall back to inference from the tool name, so a vendor renaming a category can't silently
drop a known tool out of the pitch. `python execution/visitor_identification/test_enrichment_providers.py` covers
this against each vendor's documented response shape.

Only `mock` is exercised in this repo. The live providers are written against their
documented request shapes but have never been run against a real key — verify one batch by
hand before trusting the output.

## The outreach — the hybrid, and the approval loop

The frameworks in `execution/shared/sales_frameworks/` are the client's own playbook and are the
authority on structure and register. They load as raw markdown into the prompt, so **editing
those files changes the output with no code change.**

They live under `shared/` because the cold campaign (`directives/cold_email_campaign.md`)
loads the same playbook — `16_cold_lead.md` is a delta on `15_warm_visitor_hybrid.md` that
replaces only the two beats assuming a website visit. **Editing a framework changes both
areas.** That is intended: it is one client voice, not two. `validate_draft` in
`generate_outreach_emails.py` is shared the same way, so run this area's tests after
touching it.

**It is a blend of both DM types, not one of them.** An identified visitor sits between
them. A pure Conversion DM assumes the sub-audience was built through prior conversation, so
it opens straight on the offer — sent to someone who has never spoken to us that reads
generic, and nothing in it proves we know anything about this company. A pure
Audience-Building DM spends its energy surfacing pain the visit already proved. The blend
takes the Audience-Building DM's sharp personalized pain hook and fuses it onto the
Conversion DM's offer and CTA. `15_warm_visitor_hybrid.md` specifies the structure; all of
`00_foundation.md`, `10_conversion_dm.md`, `20_audience_building_dm.md`,
`15_warm_visitor_hybrid.md` and `30_offer.md` load.

Structure: **warm open → personalized pain hook → one-line solution → sample-dashboard CTA
→ soft open door.** 45–85 words, hard-rejected past 105.

Non-obvious rules that came out of review:

- **Acknowledge the visit.** "Thanks for stopping by the site" is warm and wanted. Narrating
  their behaviour — which pages, how long, what they clicked — is not, and is still banned.
- **Naming tools is not a pain.** "You use HubSpot and Stripe" is an observation. The hook
  has to say what having them separately *costs*. Any sentence that would survive being sent
  to a different company unchanged gets rewritten.
- **The CTA is fixed**, not rotated: offer a sample dashboard built on their actual stack.
  Approved, low friction, and it proves the claim instead of asserting it.
- **No "are you interested?"** — a dead question, banned along with "let me know if you'd
  like". The open door must be answerable in a sentence.
- **Never lead with price** and never offer a discount. Price comes up on the walkthrough.

### Variants and the approval loop

`execution/visitor_identification/email_variants.json` defines structural approaches that differ in the **move they
make on the pain** — the line the email lives or dies on, not the tone. Run the matrix, get
the copy approved, then run the batch:

```bash
# every variant against 3 deliberately contrasting leads (a BI owner, a big stack with no
# BI, the smallest company) — the cases that break a weak variant
python execution/visitor_identification/generate_email_variants.py --run-name demo
# then set the winners in rb2b_config.json -> active_variants
```

Approved variants: `stack_math`, `monday_scramble`, `blind_spot`, `new_way`,
`question_first`, `ultra_short`. `proof_point` and `peer_frame` were reviewed and not
selected — kept, not deleted, since a variant that loses one review may win the next.

Routing is not pure rotation: **`blind_spot` always wins for a lead that already owns a BI
tool** (offering a first dashboard to a Looker shop is the fastest way to prove you didn't
look), and `ultra_short` for companies under 60 staff. Everything else rotates.

### Approved examples drive the voice

Each variant carries `approved_examples` — real drafts the client picked from a matrix —
injected as few-shot exemplars. A framework describes a register; an approved email
demonstrates it, and that is what makes new copy a derivative of an approved voice rather
than a fresh interpretation. The prompt is explicit that the other prospect's company, tools
and specifics must never be reused, and two mechanical checks enforce it (see below).

To refresh the voice: run the matrix again, pick new winners, and replace `approved_examples`.

## Grounding — the checks that matter

Every draft is validated against that lead's own enrichment record before it is written out:

- **Names at least 2 of their detected tools, and no tool they don't run** — checked against
  the detector's full vocabulary, so a hallucinated "HubSpot" on a Salesforce shop is caught.
  This also catches an exemplar's tools leaking across into a different prospect's email.
- **No sentence reproduced verbatim from an approved example** (the fixed CTA excepted —
  every email ending the same way is intended). Asking the model nicely was not enough: a
  batch of 10 came back with 4 borrowed lines, which would be conspicuous at a hundred.
- Addresses them by first name; 35–105 words; subject under 60 characters.
- No banned phrases — cold-email boilerplate, dead questions, surveillance framing.

A failing draft is regenerated once with the problems fed back. If it still fails it is kept,
flagged `needs_review`, and surfaced at the top of its dashboard card — never silently
shipped.

## Going live with real RB2B

1. In `execution/visitor_identification/rb2b_config.json` set `"mode": "live"` and your `rb2b_script_id`.
   **Paste RB2B's actual snippet** from their Script Setup page over `LIVE_PIXEL` in
   `rb2b_site.py` — ours is a faithful reconstruction from their documented CDN path and the
   standard loader-stub pattern, but their real bytes are minified and may differ.
2. Deploy the Flask app to a public HTTPS host. RB2B cannot reach `localhost`; ngrok is fine
   for a first test. Use a real WSGI server, not Flask's dev server.
3. Rotate `webhook_token` to something random, then set RB2B → Integrations → Webhook to
   `https://<host>/rb2b/webhook?token=<token>`. Use their Test Script tool to confirm.
4. Delete `execution/visitor_identification/rb2b_mock_resolver.py` and `execution/visitor_identification/mock_visitors.py`, and drop the
   `/mock/collect` and `/rb2b-shim.js` routes.
5. Install the pixel site-wide, not just the homepage.

The receiver, store, enrichment, drafting and dashboard are untouched. They never knew the
data was mocked.

## Edge cases / things learned

- **`requests` cannot fire the pixel.** The tracking snippet is JavaScript, so a plain HTTP
  GET loads the page and executes nothing. Traffic runs through Playwright Chromium with a
  fresh browser context per visitor — fresh context, fresh `localStorage`, distinct visitor
  id. The `--engine requests` fallback posts the beacon by hand and exercises everything
  except the JS itself; it warns loudly when it kicks in.
- **A backgrounded server swallows unflushed prints.** The resolver's decisions were
  invisible until every `print` became `print(..., flush=True)`. Same lesson as the daily ad
  runners; when stdout is a file, Python buffers and you see nothing until exit.
- **The resolver's webhooks land *after* the traffic script exits.** Matching is async with a
  delay, so "traffic finished" and "all webhooks arrived" are different moments. The
  orchestrator polls `/health` until the identified count stops climbing rather than sleeping
  a fixed amount, and only then stops the server — which is also what keeps the in-flight
  resolver threads alive long enough to land.
- **RB2B's own documented `Seen At` example is malformed ISO** (`2024-01-01T12:34:56:00.00+00.00`
  — colons where the fractional seconds belong). `parse_seen_at` keeps the raw string when it
  can't parse rather than dropping a lead over a timestamp.
- **Ad attribution needs no side channel.** UTMs ride in RB2B's `Captured URL` and are parsed
  back out during enrichment, exactly as they would in production.
- **Segment-blind copy destroys credibility.** The first generated batch offered Priya Raman
  "one dashboard" when enrichment had already detected Looker at her company. Telling someone
  they need the thing they already own says you didn't look. The BI tool now drives a hard
  requirement in the prompt, not a side note, and the offer becomes feeding the tool they have.
- **One framework plus ten leads converges.** At temperature 0.9 the first batch opened and
  closed nearly identically every time — eight of ten ended "Would you be open to a quick
  20-minute chat". Entry angles and CTA shapes now rotate on coprime strides (6 and 5), which
  is the playbook's own reoffer principle applied across a batch instead of across weeks.
  `"would you be open to"` is also a banned phrase.
- **Gemini's SDK warns about automatic function calling** on every `generate_content` call.
  We pass no tools, so `AutomaticFunctionCallingConfig(disable=True)` silences it.
- **The mock resolver logs a non-US skip per pageview**, not per visitor, because the geo
  check runs before the repeat-visit check. Noisy but informative; left as is.
- **An example in a prompt gets copied verbatim.** The first hybrid batch opened every
  email with "Thanks for stopping by the site" and closed every one with an identical line —
  both lifted straight from the examples in the prompt. Examples teach register, but the
  model treats them as text to reuse unless told otherwise and checked. Now the prompt gives
  a range and forbids reuse, and `exemplar_sentences()` rejects verbatim matches outright.
- **A provider-level failure must not overwrite good output.** Running `--provider salesnav`
  raised inside the per-lead `try/except`, so all 10 leads "failed individually", the loop
  logged ten errors, and the script then wrote an empty `enriched.json` over a good run.
  `NotImplementedError` and `RuntimeError` (unwired provider, missing API key) now abort
  immediately, and a batch where every lead failed refuses to write at all. The general
  lesson: a per-item guard should never swallow a systemic failure.
- **That lesson was learned in enrichment and not carried into drafting.** With no
  `GEMINI_API_KEY` every draft failed individually, `generate_outreach_emails` wrote an empty
  `drafts.json` over a good one and exited 0, and the orchestrator carried the emptiness into
  the CSV export and the dashboard as though it were a real result. It now refuses to write a
  batch that produced nothing and exits non-zero, matching `build_cold_emails`. Worth
  checking, whenever a guard like this is added, whether its sibling script needs it too.
- **Drafting is gated on spend, and the gate is asked by the orchestrator.** Drafting is the
  only paid stage here. `run_visitor_pipeline` captures its child's output and replays it
  after the child exits, so a gate prompt asked down there would hang with nothing on screen
  to explain why. The pipeline gates once, using the enrichment count, and forwards
  `--yes-spend`. A default 10-visitor run sits at the free-pass threshold and is never
  interrupted; the gate engages on the larger runs, which is where the money is.
- **Drive upload is best-effort.** A missing or expired `token.json` logs and continues rather
  than voiding a run that already produced everything of value.
- **Everything is fictional and nothing sends.** People, companies, domains and email
  addresses are invented; the pipeline only writes `.eml` files. There is no send path, by
  design — adding one is a separate decision.
