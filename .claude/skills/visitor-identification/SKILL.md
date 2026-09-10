---
name: visitor-identification
description: Run the RB2B-shaped visitor identification pipeline — serve the Unified Dashboards site, drive mock visitors through it, resolve them to named people via a real webhook, enrich each with their tech stack, and draft a personalized outreach email per lead using the client's sales frameworks. Use when the user asks to identify website visitors, run RB2B, see who landed on the site, generate leads from traffic, enrich visitors, draft outreach emails to visitors, or run the visitor / lead pipeline.
---

# Visitor Identification

Turns anonymous site traffic into named, enriched leads with an outreach email drafted for
each one: **ad click → identified person → tech stack → drafted email → review dashboard.**

Everything downstream of the webhook is production code written against RB2B's real
contract. Only the identity resolution is mocked. **Not the same as `ad-creator` or
`image-ad-creator`** — this pipeline consumes hand-authored ad *references* and must never
invoke the creative generation scripts.

## Steps

1. **Read `directives/visitor_identification.md` first.** It is the source of truth: the
   verified RB2B contract, the workflow, the cutover steps and the accumulated edge cases.
   Don't improvise around it.

2. **Get the inputs from the user.** How many visitors (default 10); whether to include the
   2 non-US sessions that demonstrate RB2B's US-only filter; whether to actually generate
   emails (spends Gemini tokens) or use `--dry-run`; match rate (default `1.0` so all
   visitors resolve — `0.3` behaves like production RB2B).

3. **Run the pipeline:**
   ```
   python execution/visitor_identification/run_visitor_pipeline.py --run-name <run> --visitors 10 --include-international --fresh
   ```
   It boots the site, drives real browser sessions through it, waits for the delayed
   webhooks to settle, enriches, drafts, builds the dashboard and uploads to Drive. Parse
   its `KEY=value` output lines for the results.

4. **Check the flagged drafts.** `NEEDS_REVIEW=` counts drafts that failed grounding — a
   named tool the company doesn't run, a banned phrase, wrong length. They are kept and
   surfaced at the top of their dashboard card. Read those before reporting success.

5. **Hand back the dashboard, the CSV and the Drive link.** `DASHBOARD_PATH=` is the
   review surface, `CSV_PATH=` is the same data flat (40 columns, one row per lead) for
   spreadsheet review, `DRIVE_FOLDER_LINK=` is the deliverable. Summarize: sessions,
   identified, drafted, flagged.

6. **To change how the emails read, edit `execution/shared/sales_frameworks/*.md`, not the script.**
   Loaded as raw markdown into the prompt. `15_warm_visitor_hybrid.md` is the structure the
   generator follows, `30_offer.md` is what's being offered.

7. **If the user wants different copy, run the approval loop — don't tune the prompt blind:**
   ```
   python execution/visitor_identification/generate_email_variants.py --run-name <run>
   ```
   It writes every variant against 3 contrasting leads. Publish the review page, let them
   pick, then set the winners as `active_variants` in `rb2b_config.json` and copy their
   chosen drafts into that variant's `approved_examples` in `execution/visitor_identification/email_variants.json`
   — those exemplars are what make new copy sound like the approved copy.

8. **For real tech-stack data, use `--provider apollo` or `--provider builtwith`** and add
   the key to `.env`. See `execution/shared/enrichment_providers.py`. If the user asks for
   LinkedIn Sales Navigator, tell them it cannot supply technographics via API — SNAP is
   closed to new partners and doesn't expose them to third parties regardless.

## Reference files

- `directives/visitor_identification.md` — SOP + the verified RB2B contract (source of truth)
- `execution/visitor_identification/run_visitor_pipeline.py` — end-to-end orchestrator
- `execution/visitor_identification/rb2b_site.py` — the site + the **production** webhook receiver
- `execution/visitor_identification/rb2b_mock_resolver.py` — the only mocked component; deleted on cutover
- `execution/visitor_identification/mock_visitors.py` — browser-driven traffic; deleted on cutover
- `execution/visitor_identification/enrich_visitors.py` — tech stack + ad attribution
- `execution/shared/enrichment_providers.py` — **the provider seam**: mock / apollo / zoominfo / builtwith / clearbit
- `execution/visitor_identification/export_leads_csv.py` — flat CSV of every lead, its enrichment and its draft
- `execution/visitor_identification/generate_outreach_emails.py` — framework-driven drafting with grounding checks
- `execution/visitor_identification/generate_email_variants.py` — the approval loop: every variant x contrasting leads
- `execution/visitor_identification/email_variants.json` — variant definitions + the client's approved examples
- `execution/visitor_identification/build_leads_dashboard.py` — the review surface
- `execution/visitor_identification/test_rb2b_receiver.py` — RB2B contract tests
- `execution/visitor_identification/test_enrichment_providers.py` — provider/category-mapping tests
- `execution/visitor_identification/rb2b_config.json` — mode, script id, webhook token, match rate
- `execution/shared/sales_frameworks/` — the client's playbook (edit these to change the copy)
- `execution/visitor_identification/mock_data/` — personas and hand-authored ad references
- `execution/shared/tech_stacks.json` — the canonical category and BI-tool vocabulary, shared with the cold pipeline
