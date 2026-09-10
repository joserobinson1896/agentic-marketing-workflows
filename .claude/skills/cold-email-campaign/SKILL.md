---
name: cold-email-campaign
description: Run the whole cold outreach pipeline - source a lead list, enrich it with each company's tech stack, write one personalized cold email per lead into a CSV column, and build an Instantly (API v2) campaign that uses that column as a per-lead custom variable so every lead gets a different email. Use when the user asks to run a cold email campaign, scrape or source leads, enrich a lead list, personalize a lead list, add an email column to a CSV, set up Instantly, build a campaign from leads, or push leads to Instantly.
---

# Cold Email Campaign

**scrape → enrich → write copy → build the Instantly campaign (paused).**

The whole design is one idea: the campaign sequence contains **no copy**. Its subject is
`{{email_subject}}` and its body is `{{personalized_email}}`, and each lead carries its own
values as Instantly custom variables. One campaign, one step, N genuinely different emails.
Nothing is spun, merged or templated at send time.

**Not the same as `visitor-identification`** — that one writes to people who visited the
site and never sends anything. This one writes to strangers and ends in a real campaign.
They share `execution/shared/sales_frameworks/` and `execution/shared/enrichment_providers.py`,
so editing a framework or adding a vendor changes both.

## Read this first

**`directives/cold_email_campaign.md` is the source of truth.** It carries the verified
Instantly v2 contract, the client's copy rules and why each exists, the safety defaults and
the edge cases. Don't improvise around it, and **don't re-derive the API from their docs
site** — the human docs are a JS app that 404s any fetch. The OpenAPI JSON named in the
directive is the only machine-readable source.

## The one-command path

```bash
python execution/cold_email/run_cold_pipeline.py --run-name <run> \
    --source <lead csv> --campaign-name "<name>"
```

It stops the moment a stage fails, which is the point: a failed enrichment must never hand
a stackless CSV to generation, which would spend a hundred model calls writing generic copy.
Skip stages when iterating: `--from copy` reuses the enriched CSV, `--stop-after copy`
writes the copy without touching Instantly.

Every stage also runs standalone, and usually should the first time through.

## Stage 1 — source the leads

```bash
python execution/cold_email/scrape_leads.py --provider csv --source <path> --run-name <run>
python execution/cold_email/scrape_leads.py --list-providers
```

**This is the seam a real scraper plugs into, and it is the only stubbed step in the
pipeline.** `csv` is wired and is what runs today; `apollo`, `apify` and `clay` are
documented stubs carrying each vendor's actual request shape. They **raise** rather than
return placeholder rows, because a scraper that silently invents leads is worse than one
that fails. Wiring one means writing a `fetch(query, limit)` and registering it — nothing
downstream knows where rows came from.

A provider must return `LEAD_COLUMNS`. Three are load-bearing: `email` (addresses the send,
and Instantly dedupes on it), `first_name` (opens every email), and `company_domain`
(**what enrichment looks the stack up by, and the stack is what makes the copy personal**).
Leads on a personal email domain are dropped by default; `--allow-freemail` keeps them.

## Stage 2 — enrich

```bash
python execution/cold_email/enrich_leads.py --run-name <run> --provider passthrough
python execution/cold_email/enrich_leads.py --list-providers
```

**Enrichment coverage is the ceiling on copy quality.** Every variant's pain hook is built
from named tools, the grounding check rejects a draft naming fewer than two, and a lead with
no stack falls into `no_stack_detected` where the prompt forbids naming any tool at all.

- `passthrough` (default) — the CSV already carries `tech_stack`, as an Apollo or Clay
  export usually does. Re-normalizes it into our category vocabulary rather than trusting
  the vendor's.
- `apollo` / `builtwith` / `clearbit` — real, need a key in `.env`.
- `mock` — the offline fixture. It only knows the visitor demo domains, so on a real list
  it resolves nothing.

Watch `COVERAGE=`. Under 60% it warns; at 0% it **refuses to write the file**, so a failed
run cannot overwrite a good enriched CSV with a stackless one.

## Stage 3 — write the copy

**This stage spends money and is gated.** `spend_gate.py` prints an estimate and refuses a
non-interactive batch without `--yes-spend`. Never pass that flag on your own initiative:
CLAUDE.md requires checking with the user before paid calls, including when re-running a
batch to test a fix. Show the estimate, get an answer, then pass it.

**Smoke test before the full list and show the user real drafts.** Runs of 10 or fewer are
under the gate and cost little; this is how you avoid spending, not how you spend.

```bash
python execution/cold_email/build_cold_emails.py --run-name smoke --limit 7
python execution/cold_email/build_cold_emails.py --run-name <run> --workers 8
```

The cold `approved_examples` are *derived* from approved warm copy, not themselves approved.
Read 7 and offer changes before spending the batch. Check `NEEDS_REVIEW=` on the full run:
those failed grounding twice and were kept anyway.

Generation is parallel (`--workers`, default 8): ~3 minutes for 100 instead of ~100.
Variants are assigned before any call, so the batch is reproducible; results fill by index,
so the CSV keeps the input's row order. Lower `--workers` if the API rate limits.

## Stage 4 — build the campaign

```bash
python execution/cold_email/instantly_client.py --list-accounts   # verify the key FIRST
python execution/cold_email/create_instantly_campaign.py --name "<name>" --text-only
```

Prompt for `INSTANTLY_API_KEY` **here, not at the start** — everything above runs without it.
A 401 almost always means a v1 key or v1 auth style. **`ACCOUNTS=0` means the campaign will
build and then sit inert** — it validates, imports and can never send. Say so before
building, not after. `--dry-run` prints both payloads and calls nothing.

**Never pass `--activate` on your own initiative.** It mails real people and cannot be
recalled. Building the campaign and starting it are separate decisions and the user makes
the second one. Hand back `CAMPAIGN_URL=` and let them review it.

## Changing copy on a campaign that already exists

```bash
python execution/cold_email/update_instantly_leads.py --dry-run
python execution/cold_email/update_instantly_leads.py --csv <personalized csv>
```

`POST /leads/add` is an **import**, not an upsert: against emails already in the campaign it
skips or duplicates them and never updates copy. Rewriting copy is `PATCH /leads/{id}`,
which needs the UUID that only `POST /leads/list` returns. This script does all three steps
and refuses to touch a campaign that is actively sending.

## Changing how the emails read

Edit the frameworks and variants, **not the script** — both load at runtime.

- `execution/shared/sales_frameworks/16_cold_lead.md` — the cold delta on the approved warm
  structure, and the home of the client's three hard rules: **no em dashes, never state how
  many systems they run, one call to action.**
- `execution/cold_email/cold_email_variants.json` — per-variant `pain_move` and examples.

After changing a check, revalidate instead of regenerating. Free, and it rewrites the flags
in both `drafts.json` and the CSV:

```bash
python execution/cold_email/build_cold_emails.py --run-name <run> --revalidate
```

## Tests

```bash
python execution/cold_email/test_pipeline.py      # sourcing + enrichment seams
python execution/cold_email/test_cold_email.py    # copy rules, segmentation, payloads
```

Run `execution/visitor_identification/test_rb2b_receiver.py` and
`test_enrichment_providers.py` too if you touched `validate_draft` or anything in `shared/`
— both are shared with the visitor pipeline.

## Reference files

- `directives/cold_email_campaign.md` — SOP + verified Instantly v2 contract (source of truth)
- `execution/cold_email/run_cold_pipeline.py` — the four stages in one command
- `execution/cold_email/scrape_leads.py` — **the scraper seam**; `--list-providers`
- `execution/cold_email/enrich_leads.py` — fills `tech_stack`; coverage gate
- `execution/cold_email/build_cold_emails.py` — writes the copy, adds the CSV columns
- `execution/cold_email/create_instantly_campaign.py` — builds the campaign, imports leads
- `execution/cold_email/update_instantly_leads.py` — rewrites copy on existing leads
- `execution/cold_email/instantly_client.py` — the v2 API client; `--check` verifies a key
- `execution/cold_email/cold_email_variants.json` — variant definitions + cold examples
- `execution/shared/sales_frameworks/` — the client's playbook, shared with the visitor pipeline
- `execution/shared/enrichment_providers.py` — the tech-stack vendor seam, shared
