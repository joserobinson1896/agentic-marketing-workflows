# Cold Email Campaign

## Goal

Source a lead list, enrich it with each company's tech stack, write one genuinely different
email per lead, put the copy back into the CSV as a column, and build an Instantly campaign
whose entire body is that column.

The chain: **scrape → enrich → personalized copy column → Instantly campaign, one email per
lead.** `run_cold_pipeline.py` runs all four and stops the moment one fails.

Only the first stage is stubbed, and deliberately: sourcing needs a paid data vendor, so
`scrape_leads.py` is a provider seam with `csv` wired and Apollo/Apify/Clay documented.

The point of the column is that it becomes an Instantly **custom variable**. The campaign
sequence contains no copy at all — its subject is `{{email_subject}}` and its body is
`{{personalized_email}}`. Nothing is spun, merged or templated at send time. A hundred leads
get a hundred emails that were each written for them.

## How this differs from the other directives

| | `visitor_identification.md` | **this** |
|---|---|---|
| Audience | People who visited our site | People who have never heard of us |
| Trigger | An RB2B webhook fires | A scrape runs, or a CSV exists |
| Stack data | Detected live by an enrichment provider | Same providers, via `enrich_leads.py` |
| Copy | Warm hybrid (`15_warm_visitor_hybrid.md`) | Cold delta (`16_cold_lead.md`) |
| Deliverable | Review dashboard + `.eml` drafts | Personalized CSV + a built Instantly campaign |
| Sends? | No — no send path exists, by design | **Yes, eventually.** Built paused; a human activates it. |

These two areas share `execution/shared/sales_frameworks/` and
`execution/shared/enrichment_providers.py`. Editing a framework changes both voices, and
adding a tech-stack vendor serves both pipelines. That is intended: it is one client voice
and one enrichment seam, not two of each.

## The copy is the approved script, minus two beats

The warm visitor email was approved out of a variant matrix. **It was not rewritten for
cold.** `16_cold_lead.md` records the delta, and it is deliberately small:

| Beat | Warm | Cold |
|---|---|---|
| 1. Open | "Thanks for stopping by the site." | A flat, specific observation about them |
| 2. Pain hook | their stack → the consequence | **unchanged** |
| 3. Solution, one line | | **unchanged** |
| 4. CTA, sample dashboard on their stack | | **unchanged, and now the LAST line** |
| 5. Open door | "What brought you to the site?" | **deleted** |

Beats 2-4 are what got approved and they never referred to the visit. Rewriting more than
beats 1 and 5 would throw away the thing that was working.

### The three rules from the client's copy review

Round one was rejected on three specific points. All three are now **enforced in code**, not
merely requested in the prompt, because a prompt instruction is followed most of the time and
"most of the time" across a hundred sends is a mess. Each one is a hard reject in
`check_cold_rules()`, each has a test, and the approved examples were rewritten so the model
is not imitating a violation.

**1. No em dashes, ever, in any sales copy.** Em and en dashes are swapped for commas by
`normalize_typography()` *before* the draft is validated, so the fix costs no regeneration,
and any that survive are rejected. Normalizing before validating rather than after is the
detail that matters: the other order spends an API call fixing something the normalizer was
about to handle.

**2. Never state how many systems they run.** Not "nine distinct reporting systems", not
"three different answers". Two reasons, and the second is the one that decides it:

- It reads as intrusive, like a count taken from somewhere it shouldn't have been.
- **It is an easy way to be wrong.** The detected stack is only what is publicly visible.
  They run tools we cannot see, so any specific number is probably false, and being
  confidently wrong in line two ends the email.

Name the tools, describe the consequence, leave the quantity vague. This killed `stack_math`'s
original premise, which was explicitly "the number of systems IS the pain"; its `pain_move`
was rewritten to make the *disagreement between* their systems the pain instead.
The check masks the lead's own tool names out of the text first, so a product name carrying
a numeral (**Google Analytics 4** is the one on this list) is not read as a count.

**3. One call to action. Never two.** The warm script's soft open door after the CTA is a
second ask on a cold lead, and two asks split the reply between them until neither is the
obvious thing to do. The CTA is now the last thing the email says; anything after it other
than a sign-off is rejected, and the phrasings that hide inside the CTA's own paragraph
("or tell me...", "or let me know...") are banned outright.

A fourth rule came out of the same review by inference rather than instruction: **naming
their tools is fine, announcing an inventory of them is not.** "Your tech stack includes X,
Y and Z" is the same intrusiveness as counting, spelled differently, because it frames the
email as a report about them. Banned.

Removing beat 5 shortened everything, so the target band moved from 45-85 words to
**40-75**.

The same six variants run, with the same `pain_move` text:
`stack_math`, `monday_scramble`, `blind_spot`, `new_way`, `question_first`, `ultra_short`.
Only their `approved_examples` were rewritten to open cold — and those examples are
**derived from approved copy, not themselves approved.** Run a small batch and read it
before trusting them at volume.

### Generation runs in parallel

A draft takes about 12 seconds and essentially all of it is spent waiting on the model, so
the work is IO-bound and threads are the right tool. `--workers` (default 8) puts the batch
through a `ThreadPoolExecutor`. **100 drafts went from ~100 minutes to 2.8.**

Three things make that safe rather than merely fast:

- **Variants are assigned before any call is made**, so which lead gets which variant does
  not depend on completion order and the batch stays reproducible.
- **Results are filled by index, not appended**, so the CSV keeps the input's row order
  however the futures happen to land. Appending would silently shuffle the file.
- **Rate limits back off instead of counting as a rejected draft.** `generate_for_lead`
  allows two attempts total, so without `call_gemini_with_backoff` two 429s in a row would
  be recorded as a lead that could not be written: a silent hole in the batch. Backoff is
  exponential with jitter, and the jitter is what stops every worker retrying in lockstep.

8 workers has run clean with no rate limiting. Lower it if the API starts pushing back;
the model parameters are deliberately untouched, because those produced the approved copy.

### Variant routing is gated, not just preferred

`blind_spot` always wins for a lead that already owns a BI tool. That much matches the warm
pipeline. What the warm pipeline does *not* do, and this one must, is stop the **rotation**
from handing `blind_spot` to a lead with no BI tool — its whole move is "you already have
the dashboard, the gap is what feeds it", which is simply false for them, and the segment
directive in the same prompt says the opposite. `requires_bi` in
`cold_email_variants.json` gates the eligible pool per lead. On this list it is the
difference between 43 correct `blind_spot` sends and 52 with 9 lies in them.

### Colleagues at one company get different variants

**20 of the 100 companies on this list have two contacts**, and colleagues compare notes.
Two people at one company drawing the same variant produces near-identical emails on the
same morning, which is the most visible automation tell there is. The first batch sent
Quinton Components' sales manager and head of marketing subject lines that differed by one
word, and Juniperline and Dovetail were nearly as close.

So a company's second contact takes a different variant. This overrides the **routing**,
never the `requires_bi` **gate**: the gate is a truth constraint (`blind_spot` asserts they
own a BI tool) and is never crossed, while the routing is only a preference, and the segment
directive in every prompt still forbids pitching a first dashboard to a company that already
has one whichever variant runs. The first contact keeps the routed variant; the colleague
rotates onto the next eligible one.

Effect on this list: `blind_spot` fell from 43 to 35, and all 100 subjects are now distinct.
Note that Instantly's `stop_for_company` is on, so a reply from either contact stops the
campaign for the whole domain.

## Inputs needed from the user

- The lead CSV. Needs at minimum `email`, `first_name`, `company_name`, `tech_stack`.
- `INSTANTLY_API_KEY` in `.env` — **a V2 key**, from Settings → Integrations → API Keys.
- Which sending mailboxes to use (`--sending-accounts`), if not set later in the UI.
- HTML or plain text (`--text-only`). Plain text is the default recommendation for a cold
  first touch.

## The scraper seam — the only stubbed step

`execution/cold_email/scrape_leads.py` is where a real lead source plugs in. Everything
downstream of it runs against real APIs; sourcing is stubbed because it is the one step that
needs a paid data vendor. The architecture copies `enrichment_providers.py` deliberately:
one function, one signature, register and go.

    fetch(query, limit) -> [rows in LEAD_COLUMNS shape]

| Provider | State | Notes |
|---|---|---|
| `csv` | **wired** | Reads an exported list. Not a toy: most lists arrive as a CSV out of Apollo, Clay or Sales Navigator, so this is what runs in practice |
| `apollo` | documented stub | `POST api.apollo.io/api/v1/mixed_people/search`, header `x-api-key`. **Best first vendor**: returns the person AND `organization.current_technologies[]` in one call, so it can serve sourcing and enrichment both. Emails come back as `email_not_unlocked@domain.com` until revealed, which costs credits — filter those or you will mail a placeholder |
| `apify` | documented stub | Async: `POST /v2/acts/<id>/runs`, poll the run, then read the dataset. Not a single request |
| `clay` | documented stub | **Push-shaped, not pull-shaped.** No "give me the table" endpoint on most plans; it needs a webhook receiver, and `rb2b_site.py` is the one to copy |

**The stubs raise, they never return placeholder rows.** A scraper that silently invents
leads is worse than one that fails, because the failure surfaces at send time.

Three columns are load-bearing. `email` addresses the send and is Instantly's dedupe key,
`first_name` opens every email, and **`company_domain` is what enrichment looks the stack up
by** — a row without one produces a generic email. `company_domain` falls back to the email's
domain when a provider omits it. Leads on a personal email domain (gmail, outlook, …) are
dropped by default, since there is no company to enrich.

## Enrichment is the ceiling on copy quality

`execution/cold_email/enrich_leads.py` fills `tech_stack`. This is not a nice-to-have step:
every variant's pain hook is built from named tools, the grounding check rejects a draft
naming fewer than two of them, and a lead with no stack lands in `no_stack_detected` where
the prompt forbids naming any tool at all. **Thin enrichment produces generic copy, and no
amount of framework tuning fixes it.**

It runs through `execution/shared/enrichment_providers.py` — the same seam the visitor
pipeline uses, moved to `shared/` when this area started needing it, so adding a vendor
serves both. `passthrough` is the default and re-normalizes a column the export already
carries into our category vocabulary rather than trusting the vendor's.

Two guards, both learned the hard way:

- **Coverage is reported, and 0% refuses to write the file.** A systemic failure (wrong
  provider, dead key, vendor returning empty for every domain) would otherwise overwrite a
  good enriched CSV with a stackless one, and the damage shows up much later as a batch of
  generic copy. This is the same lesson as the visitor pipeline's "a per-item guard must
  never swallow a systemic failure". `--allow-empty` overrides.
- **Lookups are cached per company.** 20 of the 100 companies on the reference list have two
  contacts; a naive loop pays the vendor twice for one domain and can get two different
  answers, which would give colleagues contradictory emails.

## Workflow

```bash
# 0. The whole pipeline in one command. Stops the moment a stage fails, so a bad
#    enrichment never reaches generation and never spends model calls.
python execution/cold_email/run_cold_pipeline.py --run-name <run> \
    --source <lead csv> --campaign-name "<name>"
#    Iterating on copy? --from copy reuses the enriched CSV. --stop-after copy skips Instantly.

# 1a. Source the leads (the scraper seam)
python execution/cold_email/scrape_leads.py --provider csv --source <path> --run-name <run>
python execution/cold_email/scrape_leads.py --list-providers

# 1b. Fill the tech stack. Watch COVERAGE=.
python execution/cold_email/enrich_leads.py --run-name <run> --provider passthrough

# 1. Look at the prompts and the variant split without spending anything
python execution/cold_email/build_cold_emails.py --dry-run

# 2. Smoke test — read real copy before committing to the whole list
python execution/cold_email/build_cold_emails.py --run-name smoke --limit 7

# 3. The full batch, in parallel (~3 min for 100). Writes <name>_personalized.csv
#    next to drafts.json. Lower --workers if the API starts rate limiting.
python execution/cold_email/build_cold_emails.py --run-name mach100

# 4. Confirm the key authenticates and a mailbox is connected
python execution/cold_email/instantly_client.py --list-accounts

# 5. Inspect the exact payloads. No API calls.
python execution/cold_email/create_instantly_campaign.py --dry-run --text-only

# 6. Build it. Creates the campaign PAUSED and imports the leads.
python execution/cold_email/create_instantly_campaign.py \
    --name "Mach 100 — Unified Dashboards Cold" --text-only

# 7. Re-check an existing batch against the current rules. Spends nothing — use this
#    after tightening or loosening a check instead of paying to regenerate.
python execution/cold_email/build_cold_emails.py --run-name mach100 --revalidate

# 8. Change the copy on leads ALREADY imported (after a copy revision).
#    Regenerate the batch first, then push the new columns onto the existing leads.
python execution/cold_email/update_instantly_leads.py --dry-run
python execution/cold_email/update_instantly_leads.py --csv .tmp/cold_email/<run>/<name>_personalized.csv

# 9. Tests — run after touching copy rules, segmentation, the seams or either payload
python execution/cold_email/test_pipeline.py
python execution/cold_email/test_cold_email.py
```

Outputs land in `.tmp/cold_email/<run-name>/`: `<list>_personalized.csv` + `drafts.json`.

**The reference list ships with the repo** at `execution/cold_email/sample_leads_100.csv`, and
it is the default for `--leads`. It lives beside the scripts rather than in `.tmp/` because
every claim this directive makes about distribution is measured against *that* list — the 35
`blind_spot` sends, the 20 two-contact companies, the 100 distinct subjects — and a fresh
clone that cannot reproduce them cannot check them. Fully fictional: invented companies,
reserved 555-01xx phone numbers.

### The columns added to the CSV

| Column | What it is |
|---|---|
| `personalized_email` | The email body, plain text. **The personalization variable.** |
| `personalized_email_html` | The same body with `<br>`, for an HTML campaign |
| `email_subject` | Per-lead subject, also a variable |
| `email_variant` | Which variant wrote it — group by this to compare reply rates |
| `email_word_count` | Sanity column for a human scanning the sheet |
| `email_needs_review` | `yes` if it failed a grounding check and was shipped anyway |

Both body columns are always written, so switching between HTML and plain text later is a
flag on the campaign script, never a regeneration.

## Instantly API — V1 IS DEPRECATED

The only machine-readable source is their OpenAPI document. **The human docs are a JS app
that returns 404 to any fetch**, so do not try to scrape the pages:

    https://developer.instantly.ai/api-reference/openapi.json

| Fact | Consequence |
|---|---|
| Base is `https://api.instantly.ai/api/v2` | v1 paths are gone |
| Auth is `Authorization: Bearer <key>` | v1 passed `api_key` as a **query parameter**. A v1-style call 401s with no useful message — this is the single most likely reason a port "mysteriously" fails |
| A v1 key does not work on v2 | Generate a new one under Settings → Integrations → API Keys |
| `POST /campaigns` requires only `name` and `campaign_schedule` | Everything else is optional, but a campaign with no `email_list` will never send |
| `campaign_schedule.schedules[]` each require `name`, `timing`, `days`, `timezone` | All four, or 400 |
| **`America/New_York` is NOT in the timezone enum** | Eastern is spelled **`America/Detroit`**. The obvious value fails validation |
| `days` is an object keyed `"0"`–`"6"` | The spec's own example sets 0–4 true and 5–6 false, which only reads as a working week if **0 is Monday**. Inferred, not documented — eyeball the schedule in the UI after the first create |
| `sequences` is an array but **only the first element is used** | One sequence, steps inside it |
| A step requires `type` (always `"email"`), `delay`, `variants` | `delay` is the wait before the NEXT step, so it is inert on a one-step campaign but still required |
| `POST /leads/add` takes **max 1000 leads** per call | `instantly_client.add_leads` chunks, so callers never think about it |
| `custom_variables` values must be **string, number, boolean or null** | Objects and arrays are rejected outright. Flatten everything |
| Adding a custom variable to one lead updates the campaign to allow it on all of them | The campaign body can reference `{{personalized_email}}` before every lead has one |
| `POST /campaigns/{id}/activate` takes no body | **This starts sending.** |

### Changing the copy on leads that are already imported

This is a different job from importing, and the obvious call does not do it.

| Fact | Consequence |
|---|---|
| **`POST /leads/add` is an import, not an upsert** | Run against emails already in the campaign it either skips them (which it must, or the same person is mailed twice) or duplicates them. It never updates copy |
| Updating a lead is `PATCH /leads/{id}`, which needs the lead's **UUID** | The UUID is not in our CSV. It only comes back from `POST /leads/list` |
| `POST /leads/list` is a **POST** | Their own note says the filters are too complex for query parameters. It is the one endpoint that deviates from their REST shape |
| Paging is a cursor (`next_starting_after`), not an offset | No page count to compute, no skipping ahead. `list_leads` follows it and guards against a cursor that stops advancing |
| Custom variables come back on a listed lead under **`payload`** | Not under `custom_variables`. That is the write-side name only |
| **A custom variable whose name collides with an Instantly lead field is folded into that field, camelCased, and the name we sent is dropped** | Verified live: `job_title` comes back as `jobTitle` and `payload["job_title"]` does not exist. Anything referencing `{{job_title}}` would render empty. `INSTANTLY_SYSTEM_FIELDS` maps the known collisions. The variables carrying the copy collide with nothing and round-trip verbatim |
| The docs do not say whether `custom_variables` merges or replaces | So `update_instantly_leads.py` always sends the **complete** object, which makes the answer irrelevant. It imports `build_lead_payload` from the campaign builder rather than re-deriving the names, because a rename in one file and not the other sends 100 emails with an empty body |

`update_instantly_leads.py` refuses to touch a campaign whose status is `1` (actively
sending) without `--force`: rewriting copy underneath an in-flight send means some prospects
get the old version and some the new, with no record of which.

## Safety — money

**CLAUDE.md already required this and it was violated twice in one session**, so it is no
longer a rule the agent has to remember:

> Fix the script and test it again *(unless it uses paid tokens/credits/etc—in which case
> you check w user first)*

The two failures were a 100-draft batch run ahead of the user's own "5 first, then the 100"
approval gate, and a second 100-draft batch re-run **to test a fix** — which is exactly the
case that parenthetical names. A deterministic requirement was resting on probabilistic
judgment, which is the mismatch this whole architecture exists to remove.

`execution/shared/spend_gate.py` moves it into the layer that cannot forget:

| Situation | Behaviour |
|---|---|
| Interactive terminal, no flag | Prints the estimate and asks. Only a literal `yes` proceeds |
| **Not a terminal, no flag** | **Refuses and exits.** Never blocks on a prompt nothing will answer, and never reads silence as consent |
| `--yes-spend` | Proceeds, and still prints the estimate so the number is in the log |
| 10 calls or fewer | Runs freely. A smoke test is how you AVOID spending, and gating it would train everyone to pass `--yes-spend` by reflex |

The gate sits immediately before the first paid call, not at the top of `main()`: parsing,
segmentation, variant assignment and `--dry-run` are all free and must stay runnable without
approval.

**An orchestrator has to ask the question itself and forward the answer.** `run_cold_pipeline`
runs the copy stage through a pipe and streams the child's stdout back line by line. The
gate's prompt has no trailing newline, so a question asked in the child blocked the parent's
line iterator forever, on text the operator never saw — a hundred-lead run just hung. The
orchestrator now counts the enriched rows, gates once at the top where it still owns the
terminal, and passes `--yes-spend` down. `run_visitor_pipeline` does the same thing for the
same reason, except its child's output is captured rather than streamed, which would have
hidden the prompt even more completely.

**A scheduled run is already consented to** — setting up a daily batch *is* the approval for
that batch — so the daily runners pass `assume_yes` from their own config rather than being
gated every night. The gate is for ad-hoc and agent-initiated runs, which is where the
damage happened.

## Safety — this one actually mails people

Everything else in this repo produces drafts. This produces a live campaign, so:

- **The campaign is created paused.** `--activate` exists and is off by default. Building the
  campaign and starting it are different decisions and a human makes the second one.
- **Pre-flight refuses to build a campaign that would mail a blank.** Any lead with an empty
  body, an empty subject, a missing/malformed email or a duplicate email aborts the whole
  run. A hundred blank emails from your sending domain is not a recoverable mistake.
- **`skip_if_in_workspace` is on.** Re-running the import must not mail someone twice.
- **`insert_unsubscribe_header` is on.** CAN-SPAM needs a working opt-out, and putting it in
  the header rather than a footer is what lets the copy still read like a person wrote it.
- **`stop_for_company` is on.** One reply stops everyone at that domain.
- **Open and link tracking are off.** A tracking pixel and rewritten links are the two things
  most likely to put a cold first touch in spam, and open rates stopped meaning much once
  mail privacy prefetching arrived.

## Grounding — the checks that matter

Every draft is validated against that lead's own row before it is written:

- **Names no tool they don't run**, checked against the union of this list's vocabulary and
  the visitor pipeline's. A superset is strictly safer: a tool missing from the vocabulary
  is a tool the model can invent uncaught.
- **Names 2+ of their real tools from 2+ different categories.** The category spread is the
  part that carries meaning — "Google Ads and Meta Ads" is two ad platforms, not two
  reporting sources. It deliberately does *not* require the specific tools in `named_tools`;
  see the edge case below.
- **No warm language.** `COLD_BANNED_PHRASES` extends the warm list with every way of
  claiming a visit or prior contact, every way of announcing the cold email, and every way
  of explaining how we found them. This is load-bearing: `15_warm_visitor_hybrid.md` loads
  into the prompt and is full of "thanks for stopping by".
- **Beats separated by blank lines**, at least 4 paragraphs. Not a truth check — a shape
  check. A wall of text beside its neighbours in an inbox reads as a different sender.
- **No sentence reproduced verbatim from an example**, the fixed CTA excepted.
- First name present, 35–105 words, subject under 60 characters.

A failing draft is regenerated once with the problems fed back. If it still fails it is
kept, flagged `email_needs_review=yes`, and `--skip-flagged` holds it back from the import.

## When the model API refuses

A `429` is **not automatically a rate limit.** A spending cap and a dead key return the same
status, and they will still be there in thirty seconds.

    429 RESOURCE_EXHAUSTED ... "Your project has exceeded its monthly spending cap"

Retrying that wastes the full backoff on every lead: a capped project burned 40 seconds per
draft before failing, which across 100 leads is over an hour of sleeping to reach the same
answer, one failed lead at a time, looking like progress the whole way. `FATAL_MARKERS` is
checked **before** `TRANSIENT_MARKERS`; a match raises `FatalGenerationError`, the first
worker to see one sets an event, every other worker returns immediately, and the batch
stops. `"quota"` is deliberately in the transient list and not the fatal one, because it
appears in ordinary rate-limit text too.

Raise the cap at <https://ai.studio/spend>. Nothing else in the pipeline is affected: the
scrape, the enrichment and every Instantly call are unrelated to the model.

**A run that produced nothing never writes.** Generation used to exit 0 with `DRAFTED=0`,
leaving an empty personalized CSV that the orchestrator carried into the campaign stage.
Both `build_cold_emails.py` and `enrich_leads.py` now refuse to write an empty result and
exit non-zero, so a failure can never overwrite a good batch that cost real money.

## Edge cases / things learned

- **A campaign with no sending mailbox is built but inert.** `email_list` comes back `None`
  and the workspace shows 0 connected accounts. Everything validates, the leads import, the
  campaign exists — and it can never send. Check `instantly_client.py --list-accounts`
  before building, not after, and attach mailboxes in the UI or via `--sending-accounts`.
- **`named_tools` is a suggestion, not a contract.** The grounding check originally required
  2+ tools *from that curated list*, and flagged a lead whose email named HubSpot, Chargebee
  and Mixpanel — three tools genuinely on their stack — because the code had pre-picked
  HubSpot, Mailchimp and Google Ads. The email was perfect. `named_tools` picks the
  highest-signal tool per headline category to steer the prompt; the *check* has to ask the
  real question, which is whether the copy names two genuine tools from two different
  categories. The narrower version fails good copy, which trains you to ignore the flag.
- **A false-positive check is worse than no check.** Both of the above shipped as
  `needs_review` flags on correct emails. A reviewer who learns the flag is usually wrong
  stops reading it, and then the one real flag goes out with the batch.
- **A vendor name can contain another vendor's whole name.** "Shopify Plus" contains
  "Shopify", and the grounding check's word-boundary scan flagged a lead who had correctly
  named Shopify Plus for naming Shopify — "a tool they don't run". The check now masks the
  lead's own tools out of the text longest-first and scans the remainder, which asks the
  right question: what is left after removing everything they legitimately run? This bug was
  in the shared validator, so it was latent in the visitor pipeline too.
- **Gating routing is not the same as gating rotation.** See variant routing above. Picking
  the best variant for a lead leaves the fallback rotation free to pick a false one.
- **The model emits smart quotes at random** — two of seven in the first smoke batch — so a
  batch comes out typographically mixed. Straightening them is deterministic and belongs in
  code, not in a prompt instruction the model follows only sometimes. Em dashes are kept;
  the approved copy uses them deliberately.
- **"Line breaks between beats" is obeyed most of the time.** One draft in seven came back
  as 2 paragraphs where the rest of the batch ran 5–6. It is now a hard reject.
- **The lead list's categories are wider than the visitor pipeline's.** Accounting, ERP and
  Support appear here and were not in `data_source_categories`. All three hold numbers that
  belong in a revenue dashboard, so they count as reporting sources.
- **`confidence` is 1.0 for every tool.** A scraped list is an assertion, not a detection.
  There is no probability to report and inventing one would be a lie the ranking then trusts.
- **Read the CSV with `utf-8-sig`.** The scraper writes a BOM; without it the first column
  name comes back as `﻿full_name` and every `full_name` lookup silently returns empty.
- **Revalidation must be free.** Tightening a rule shouldn't mean paying to regenerate a
  hundred emails to find out what it catches, and loosening one that was flagging good copy
  shouldn't leave the flag stuck on. `--revalidate` re-runs the current checks over stored
  drafts and rewrites the flags in both `drafts.json` and the CSV. It also re-renders
  `personalized_email_html`, so a fix to the renderer reaches an existing batch.
- **Nothing about the copy needs a code change.** The frameworks are markdown loaded at
  runtime and the variants are JSON. To change how these read, edit
  `execution/shared/sales_frameworks/16_cold_lead.md` or the `approved_examples`.
