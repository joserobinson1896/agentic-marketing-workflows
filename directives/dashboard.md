# Dashboard

## Goal

Build the Unified Dashboards marketing analytics dashboard: one page that joins five platform
exports into a single view of the funnel, from media spend through to the dollars still being
billed six months later, with an AI analyst that answers questions strictly from that data.

The chain: **five CSVs → semantic layer → rendered page.**

This is the build that demonstrates what the product itself sells. Unified Dashboards is a
done-for-you reporting service that unifies scattered tool data into one live KPI view, so the
dashboard has to survive the scrutiny a prospect would apply to it.

## The split that everything else depends on

Two scripts, and the boundary between them is the whole design:

| | `build_semantic_layer.py` | `build_dashboard.py` |
|---|---|---|
| Reads | the five CSVs | the semantic layer JSON |
| Does | computes **every** number | injects it into `template.html` |
| Never | renders | computes |

**A metric is defined exactly once, in the semantic layer.** That is what makes it impossible
for the same figure to disagree with itself between two sections of the page, which is the
single most common way a dashboard loses its reader's trust. If a number looks wrong on the
page, it is wrong in `build_semantic_layer.py`, and `test_semantic_layer.py` should have
caught it. Do not fix a figure in the template.

## The seven data traps

Real platform exports disagree with each other in specific, recurring ways. All seven are
encoded in the semantic layer and marked `TRAP n` at the point they are handled. They are the
reason this build is not a `GROUP BY`.

| # | Trap | How it is handled |
|---|---|---|
| 1 | **Blank is not zero** | Instantly and RB2B carry no auction metrics, so 168 rows are blank. `num()` defaults to 0.0, and callers that must tell blank from zero pass `default=None` |
| 2 | **Right-censoring** | Retention at month N needs N months of runway before the window end. Measured against a single `WINDOW_END`, declared once rather than repeated at each call site |
| 3 | **`lost_reason` on an open deal** | 298 `contacted` rows carry a loss reason. Only a `closed_lost` deal has a real one; filter on `deal_stage`, never on the presence of the field |
| 4 | **RB2B never submits a form** | Identified traffic converts with `form_submitted = false`. A conversion definition that requires the form silently zeroes the channel |
| 5 | **The CRM is last-touch** | `crm.session_id` is the LAST session, not the first. Attribution built on it answers a different question than the one being asked |
| 6 | **Method-sensitive splits** | The sample-dashboard close-rate split changes with the counting method. Lead-level exclusive is the one used, and the choice is stated rather than left implicit |
| 7 | **Organic has no spend** | It never appears in `ad_platform_spend`, so every cost metric is undefined for it. `PAID_CHANNELS` names the paid set once, so it is excluded rather than divided by zero |

## The integrity canaries

Three cross-file joins are recomputed at the end of every build and must agree:

- weekly ad spend leads reconcile to CRM rows carrying a campaign id
- closed-won deals reconcile to distinct Stripe subscriptions
- the CRM demo-booked flag reconciles to distinct leads in the sales call log

**A failed canary refuses to emit.** Both scripts exit non-zero rather than writing a
dashboard from data that failed its own checks. This is the same principle as the cold
pipeline's "enrichment coverage of 0% refuses to write the file": a systemic data failure must
never be allowed to quietly produce a plausible-looking artifact, because the damage surfaces
much later, in front of someone who trusted it.

The canaries are recomputed, not asserted from memory. A check that hardcodes the expected
number only proves the number has not changed.

## The AI analyst

The page carries an analyst that answers questions about what is on it, through the artifact
runtime's `sample` capability.

- **The dataset is 600 KB+ and `sample()` accepts 65,536 bytes.** So the aggregates travel in
  the prompt, one round trip, and the 903 lead rows stay in the page behind a `queryAccounts`
  tool the model calls only when a question actually needs row detail.
- `build_dashboard.py` measures the brief against that ceiling on every build and **warns at
  70%**. If that warning appears, trim the brief rather than raising the ceiling.
- The system prompt binds it to the brief and the tool, never outside knowledge, and forbids
  inventing a figure.
- **Every failure mode degrades to a readable sentence**, including the capability not being
  granted at all. The dashboard is fully usable with the analyst switched off, and says so:
  every figure it would cite is already on the page.

## Injecting the data

The layer is injected into `template.html` at the `__DATA__` placeholder, inside a `<script>`
block. `</` and `<!--` are escaped in the payload first. Without that, a literal `</script>`
inside any string ends the block early and the page silently breaks from that point down.

The build also refuses to write a page over 16 MB, which is the artifact size limit.

## Workflow

```bash
# 1. Generate the five platform CSVs. Deterministic: one seed, byte-identical every run,
#    which is what makes the test suite's exact assertions meaningful.
python execution/mock_data/generate_platform_data.py

# 2. Build the page. --rebuild re-runs the semantic layer first; without it, an existing
#    layer is reused.
python execution/dashboard/build_dashboard.py --rebuild
# -> .tmp/dashboard/dashboard.html

# The semantic layer on its own, when you only want to inspect the numbers:
python execution/dashboard/build_semantic_layer.py

# 3. The math double-check. Run this after touching ANY metric definition.
python execution/dashboard/test_semantic_layer.py
```

To deliver it, publish `.tmp/dashboard/dashboard.html` as an Artifact with the `sample`
capability declared, or hand over the file for local viewing. Without that capability the page
still works and the analyst disables itself cleanly.

## Testing is a recomputation, not an assertion

`test_semantic_layer.py` does not check that the semantic layer returns what it returned last
time. It recomputes roughly fifty load-bearing figures **straight from the CSVs by a different
route** and asserts the two agree, then asserts that each of the seven traps is actually
handled rather than merely commented.

This is the "math double-check" that `CLAUDE.md` requires of any output containing
calculations. A test that imports the number it is checking proves nothing.

## Edge cases / things learned

- **The hinge column is `siloed_source_count` on `crm_deals`.** It is drawn per channel and
  then drives tier, close rate, expansion and churn hazard. Nothing downstream is random for
  its own sake, so changing a channel's source distribution in the generator moves its entire
  funnel. Do not tune one number in isolation and expect the rest to hold.
- **The channel ranking inverts as you walk down the funnel.** Meta buys the cheapest leads
  and the worst customers; LinkedIn and RB2B cost far more per lead and find companies with
  real silo pain who close on a higher tier and stay. A dashboard that stops at cost per lead
  reports the exact opposite of the truth. This inversion is the point of the build.
- **Two more findings are planted for a dashboard to surface**: booking lead time drives
  no-shows, and showing the sample dashboard on the call roughly doubles close rate with wide
  variation between reps. The second one is coachable, and it maps to the fixed call to action
  in `execution/shared/sales_frameworks/`.
- **`WINDOW_END` is a declared constant, not `today()`.** The data has a trailing edge, and a
  tenure-normalised figure measured against the wall clock drifts a little further from
  correct every day without ever looking broken.
