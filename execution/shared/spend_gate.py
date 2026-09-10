"""
Layer 3 guard: refuse to spend money without explicit consent.

WHY THIS IS CODE AND NOT A NOTE IN A DIRECTIVE:
CLAUDE.md already says it, in writing:

    "Fix the script and test it again (unless it uses paid tokens/credits/etc
     - in which case you check w user first)"

It was still violated twice in one session: a 100-draft batch run ahead of the user's
approval gate, and a second 100-draft batch re-run to TEST A FIX, which is the exact case
the parenthetical names. A rule that depends on the agent remembering it is the same
mismatch this whole architecture exists to fix - probabilistic judgment holding up a
deterministic requirement. So the requirement moves into the layer that cannot forget.

HOW IT BEHAVES:
- Interactive terminal, no flag: prints the estimate and asks. Anything but "yes" aborts.
- Not a terminal (launchd, cron, a subprocess), no flag: REFUSES and exits. It never
  blocks on a prompt nothing will answer, and it never assumes silence means consent.
- `--yes-spend` passed: proceeds and still prints the estimate, so the number is in the log.

A SCHEDULED RUN IS ALREADY CONSENTED TO. Setting up a daily batch IS the approval for that
batch, so the daily runners pass assume_yes=True from their own config rather than being
gated every night. The gate is for ad-hoc and agent-initiated runs, which is where the
damage happened.

Usage in a script that spends:

    from spend_gate import add_spend_argument, confirm_spend

    add_spend_argument(parser)
    ...
    if not confirm_spend(calls=len(rows), model=args.model,
                         label="generate cold email copy", assume_yes=args.yes_spend):
        return 1
"""

import os
import sys

# Rough per-call cost by model family, in US dollars, for THIS project's shape of call:
# a large cached-able prompt (the frameworks run 8-12k tokens) and a short JSON response.
# These are estimates for a warning, not billing. Being approximately right and visible
# beats being exactly right and absent - the point is that a number appears before the
# money leaves, not that it reconciles to the invoice.
COST_PER_CALL = {
    "gemini-2.5-flash": 0.004,
    "gemini-2.5-pro": 0.02,
    # Image generation is priced per output image, not per token, so this one is close to
    # exact rather than a rough shape estimate: ~$0.039 for a 1K image.
    "gemini-2.5-flash-image": 0.039,
    "claude-opus-5": 0.06,
    "claude-sonnet-5": 0.025,
    "claude-haiku-4-5": 0.012,
}
UNKNOWN_MODEL_COST = 0.03

# Below this, asking is noise: a smoke test of a handful of drafts is how you AVOID
# spending, and gating it would train everyone to pass --yes-spend by reflex, which
# defeats the gate on the runs that matter.
FREE_PASS_CALLS = 10


def estimate_cost(calls, model=None):
    return calls * COST_PER_CALL.get(model, UNKNOWN_MODEL_COST)


def add_spend_argument(parser):
    """Register --yes-spend on an argparse parser."""
    parser.add_argument(
        "--yes-spend", action="store_true",
        help="confirm this run may spend money on paid API calls (required when the run "
             "is not interactive)",
    )
    return parser


def confirm_spend(calls, model=None, label="make paid API calls", assume_yes=False,
                  free_pass=FREE_PASS_CALLS, stream=None):
    """Return True if the run may proceed. Prints the estimate either way.

    `assume_yes` is for a run whose consent already exists: a --yes-spend flag, or a
    scheduled job whose schedule IS the approval.
    """
    stream = stream or sys.stdout
    if calls <= 0:
        return True

    cost = estimate_cost(calls, model)
    summary = (f"  [spend] {calls} paid call(s) to {model or 'an API'} to {label}; "
               f"rough estimate ${cost:,.2f}")

    if calls <= free_pass and not assume_yes:
        print(f"{summary}  (under the {free_pass}-call gate, proceeding)",
              file=stream, flush=True)
        return True

    print(summary, file=stream, flush=True)

    if assume_yes:
        print("  [spend] approved via --yes-spend", file=stream, flush=True)
        return True

    # A non-interactive run must fail loudly rather than hang on a prompt no one will
    # answer, and must never treat "no answer" as approval.
    if not sys.stdin.isatty():
        print("  [spend] REFUSED: this run is not interactive and --yes-spend was not "
              "passed. Nothing was spent.", file=stream, flush=True)
        print("  [spend] Re-run with --yes-spend once you have approved the estimate "
              "above.", file=stream, flush=True)
        return False

    try:
        answer = input("  [spend] Proceed? type 'yes' to continue: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  [spend] REFUSED: no answer. Nothing was spent.", file=stream, flush=True)
        return False

    if answer != "yes":
        print("  [spend] REFUSED by the operator. Nothing was spent.",
              file=stream, flush=True)
        return False
    return True
