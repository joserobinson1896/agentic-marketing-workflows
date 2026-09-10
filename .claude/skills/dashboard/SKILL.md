---
name: dashboard
description: Build the Unified Dashboards marketing analytics dashboard — join five mock platform exports into a semantic layer, render the page, and verify every figure with a second-route recomputation. Use when the user asks to build or rebuild the dashboard, see marketing performance, analyze channel or funnel data, check CAC/retention/churn numbers, regenerate the platform data, or work on the semantic layer.
---

# Dashboard

Joins five platform exports into one funnel view, from media spend to the dollars still being
billed six months later, plus an AI analyst bound to that data.

**Read `directives/dashboard.md` first.** It carries the seven data traps, the integrity
canaries, the prompt ceiling and the reasoning behind the two-script split. Don't improvise
around it.

## The rule that matters most

**The semantic layer computes; the renderer only injects.** A metric is defined exactly once,
in `build_semantic_layer.py`. If a figure looks wrong on the page it is wrong there, and
`test_semantic_layer.py` should have caught it. **Never fix a number in `template.html`** —
that creates the disagreement between two sections of the page that the split exists to
prevent.

## Steps

1. **Generate the data if it isn't there.** `python execution/mock_data/generate_platform_data.py`
   writes five CSVs to `.tmp/mock_platform_data/`. Seeded, so it is byte-identical every run.

2. **Build:** `python execution/dashboard/build_dashboard.py --rebuild`
   Writes `.tmp/dashboard/dashboard.html`. Parse its output lines: section list, account row
   counts, and the AI brief size against the 64 KB prompt ceiling.

3. **Read the canary lines.** Three cross-file joins are recomputed each build. A failure
   refuses to emit and exits non-zero — that is correct behavior, not a bug to route around.
   Fix the data or the join, never the check.

4. **Heed the brief-size warning.** If the aggregates pass 70% of the prompt ceiling the build
   says so. Trim what goes into the brief; don't raise the ceiling.

5. **Run the math double-check after touching any metric:**
   `python execution/dashboard/test_semantic_layer.py`
   It recomputes ~50 figures from the CSVs by a different route and asserts agreement, then
   asserts each of the seven traps is genuinely handled. A test that imports the number it
   checks proves nothing, so keep new checks independent of the code under test.

6. **Deliver it.** Publish the HTML as an Artifact with the `sample` capability so the AI
   analyst works, or hand over the file path for local viewing. Without the capability the
   page still works and the analyst disables itself with a clear message.

## When changing the data

`siloed_source_count` on `crm_deals` is the hinge: it drives tier, close rate, expansion and
churn hazard. Changing a channel's source distribution in the generator moves that channel's
entire funnel, so re-run the tests rather than assuming a local edit stayed local.

The planted story is that the channel ranking **inverts** down the funnel — Meta buys the
cheapest leads and the worst customers. Don't flatten that while tuning; it is the finding the
dashboard exists to surface.

## Reference files

- `directives/dashboard.md` — SOP, the seven traps, the canaries (source of truth)
- `execution/dashboard/build_semantic_layer.py` — every number, computed once
- `execution/dashboard/build_dashboard.py` — renders only; injects at `__DATA__`
- `execution/dashboard/template.html` — the UI, the SVG charts and the AI analyst
- `execution/dashboard/test_semantic_layer.py` — the math double-check
- `execution/mock_data/generate_platform_data.py` — the seeded five-CSV generator
