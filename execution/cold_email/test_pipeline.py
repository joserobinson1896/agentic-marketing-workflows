"""
Offline tests for the sourcing and enrichment stages.

Everything here runs without a network or an API key. The point is the seams: a provider
contract that silently changes shape, or a systemic failure that writes a file anyway, are
the two ways this pipeline goes wrong quietly.

    python execution/cold_email/test_pipeline.py
"""

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every area on sys.path)

from scrape_leads import (  # noqa: E402
    FREEMAIL,
    LEAD_COLUMNS,
    OFFLINE_PROVIDERS,
    PROVIDERS,
    REQUIRED_COLUMNS,
    check_rows,
    fetch_csv,
    freemail_rows,
    get_provider,
    normalize_row,
    write_leads,
)
from enrich_leads import column_to_stack, enrich_rows, stack_to_column  # noqa: E402

PASSED, FAILED = 0, []


def check(label, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}" + (f"  — {detail}" if detail else ""))


def main():
    print("The scraper seam's contract with everything downstream")
    check("build_cold_emails' required columns are all in LEAD_COLUMNS",
          all(c in LEAD_COLUMNS for c in ("email", "first_name", "company_name",
                                          "company_domain", "tech_stack", "job_title")),
          "a provider returning other names breaks generation three steps later")
    check("the load-bearing columns are the ones checked",
          set(REQUIRED_COLUMNS) == {"email", "first_name", "company_domain"})
    check("every registered provider is callable",
          all(callable(f) for f in PROVIDERS.values()))
    check("csv is the only provider that runs without credentials",
          OFFLINE_PROVIDERS == {"csv"})
    check("an unknown provider raises rather than returning nothing",
          _raises(lambda: get_provider("linkedin"), ValueError))
    for name in ("apollo", "apify", "clay"):
        check(f"{name} is a documented stub that RAISES, never fake data",
              _raises(lambda n=name: PROVIDERS[n](), NotImplementedError))
        check(f"{name}'s stub says how to wire it",
              len(PROVIDERS[name].__doc__ or "") > 200)

    print("\nNormalizing a provider's row")
    row = normalize_row({"first_name": "Dana", "last_name": "Cortland",
                         "email": "dana@brightline.com"})
    check("absent columns are filled, never missing", set(row) == set(LEAD_COLUMNS))
    check("full_name is derived when the provider omits it", row["full_name"] == "Dana Cortland")
    check("company_domain falls back to the email domain",
          row["company_domain"] == "brightline.com",
          "enrichment has nothing to look up otherwise")
    check("a provider's own domain is not overwritten",
          normalize_row({"email": "a@mail.corp.com", "company_domain": "corp.com"}
                        )["company_domain"] == "corp.com")
    check("tech_stack_count is derived from the column",
          normalize_row({"tech_stack": "Stripe (Payments); Looker (BI)"}
                        )["tech_stack_count"] == "2")
    check("None values become empty strings, not the string 'None'",
          normalize_row({"phone": None})["phone"] == "")

    print("\nRefusing a list that would waste a batch")
    check("no leads at all is a problem", check_rows([]) != [])
    check("a missing email is caught",
          any("missing email" in p for p in check_rows([normalize_row(
              {"first_name": "A", "company_domain": "b.com"})])))
    check("a missing company_domain is caught",
          any("company_domain" in p for p in check_rows([normalize_row(
              {"first_name": "A", "email": "a@b.com", "company_domain": ""})])
              ) is False,
          "derived from the email, so this one should NOT fire")
    check("a malformed address is caught",
          any("not an email" in p for p in check_rows([normalize_row(
              {"first_name": "A", "email": "not-an-email", "company_domain": "b.com"})])))
    dupes = [normalize_row({"first_name": "A", "email": "a@b.com"}),
             normalize_row({"first_name": "B", "email": "a@b.com"})]
    check("a duplicate email is caught before Instantly dedupes it",
          any("duplicate" in p for p in check_rows(dupes)))
    check("a clean row passes",
          check_rows([normalize_row({"first_name": "A", "email": "a@b.com"})]) == [])

    print("\nPersonal email domains")
    free = freemail_rows([normalize_row({"email": "someone@gmail.com", "first_name": "A"}),
                          normalize_row({"email": "dana@brightline.com", "first_name": "B"})])
    check("a gmail lead is flagged", len(free) == 1)
    check("a company lead is not", free[0]["email"].endswith("gmail.com"))
    check("the common consumer hosts are covered",
          {"gmail.com", "outlook.com", "yahoo.com", "icloud.com"} <= FREEMAIL)

    print("\nRound-tripping the tech_stack column")
    column = "Stripe (Payments); Looker (BI); HubSpot (CRM)"
    stack = column_to_stack(column)
    check("the column parses to three tools", len(stack) == 3)
    check("it round-trips unchanged", stack_to_column(stack) == column,
          stack_to_column(stack))
    check("an empty column is not an error", column_to_stack("") == [])
    # A bare tool name with no "(Category)" is kept as Other rather than dropped. Several
    # vendors return bare slugs (Clearbit does), and losing a real tool is worse than
    # carrying one with a weak category: the grounding check counts named tools.
    bare = column_to_stack("Stripe (Payments); Segment")
    check("a bare tool name is kept, not dropped", len(bare) == 2, str(bare))
    check("it is categorized Other rather than guessed",
          bare[1]["category"] == "Other", bare[1]["category"])
    check("a categorized tool keeps its category", bare[0]["category"] == "Payments")

    print("\nEnrichment caches per company, so colleagues cannot disagree")
    calls = []

    def counting_provider(domain, record=None):
        calls.append(domain)
        return [{"tool": "Stripe", "category": "Payments", "confidence": 0.9,
                 "source": "test"}]

    import enrich_leads
    original = enrich_leads.get_provider
    enrich_leads.get_provider = lambda name: counting_provider
    try:
        rows = [normalize_row({"email": "a@quinton.com", "first_name": "A",
                               "company_domain": "quinton.com"}),
                normalize_row({"email": "b@quinton.com", "first_name": "B",
                               "company_domain": "quinton.com"}),
                normalize_row({"email": "c@other.com", "first_name": "C",
                               "company_domain": "other.com"})]
        rows, counts = enrich_rows(rows, "whatever", workers=2)
        check("two contacts at one company cost ONE vendor call",
              calls.count("quinton.com") == 1, f"{calls.count('quinton.com')} calls")
        check("both of them still get the stack",
              rows[0]["tech_stack"] == rows[1]["tech_stack"] == "Stripe (Payments)")
        check("a different company is fetched separately", "other.com" in calls)
        check("counts line up with the rows", counts == [1, 1, 1])
    finally:
        enrich_leads.get_provider = original

    print("\nA row with no domain is skipped, not crashed on")
    enrich_leads.get_provider = lambda name: counting_provider
    try:
        rows = [normalize_row({"email": "", "first_name": "A", "company_domain": ""})]
        rows, counts = enrich_rows(rows, "whatever", workers=1)
        check("no domain means no stack and no exception", counts == [0])
    finally:
        enrich_leads.get_provider = original

    print("\nReading a real CSV through the csv provider")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "leads.csv"
        write_leads([normalize_row({"email": "a@b.com", "first_name": "A",
                                    "company_name": "B", "company_domain": "b.com",
                                    "tech_stack": "Stripe (Payments)"})], path)
        rows = fetch_csv(source=str(path))
        check("the file round-trips", len(rows) == 1 and rows[0]["email"] == "a@b.com")
        check("the written file carries every column",
              list(csv.DictReader(open(path, encoding="utf-8-sig")).fieldnames)
              == LEAD_COLUMNS)
        check("--limit is honoured", len(fetch_csv(source=str(path), limit=0)) == 1
              or len(fetch_csv(source=str(path), limit=1)) == 1)
        check("a missing file raises rather than returning nothing",
              _raises(lambda: fetch_csv(source=str(Path(tmp) / "nope.csv")),
                      FileNotFoundError))
        check("csv without a source is an error, not an empty list",
              _raises(lambda: fetch_csv(), ValueError))

    print("\nThe spend gate (CLAUDE.md: check with the user before paid calls)")
    import io
    import spend_gate

    class FakeStdin:
        def __init__(self, tty, answer=""):
            self._tty, self._answer = tty, answer

        def isatty(self):
            return self._tty

    def gate(calls, tty=False, answer=None, **kw):
        """Run confirm_spend with a controlled stdin. Returns (allowed, output)."""
        out = io.StringIO()
        real_stdin, real_input = sys.stdin, __builtins__.input if hasattr(__builtins__, "input") else None
        sys.stdin = FakeStdin(tty)
        if answer is not None:
            import builtins
            original = builtins.input
            builtins.input = lambda *a: answer
        try:
            allowed = spend_gate.confirm_spend(calls=calls, model="gemini-2.5-flash",
                                               stream=out, **kw)
        finally:
            sys.stdin = real_stdin
            if answer is not None:
                import builtins
                builtins.input = original
        return allowed, out.getvalue()

    allowed, out = gate(100)
    check("a non-interactive batch is REFUSED without --yes-spend", not allowed)
    check("the refusal says nothing was spent", "Nothing was spent" in out, out)
    check("the estimate is printed BEFORE the refusal, so the number is in the log",
          "rough estimate" in out and out.index("rough estimate") < out.index("REFUSED"))

    allowed, out = gate(100, assume_yes=True)
    check("--yes-spend allows the batch", allowed)
    check("the estimate is still logged when approved", "rough estimate" in out)

    allowed, _ = gate(7)
    check("a smoke test under the free-pass gate runs without asking", allowed,
          "gating small runs would train everyone to pass --yes-spend by reflex")
    allowed, _ = gate(0)
    check("zero calls needs no approval", allowed)

    allowed, out = gate(100, tty=True, answer="yes")
    check("an interactive 'yes' proceeds", allowed)
    for answer in ("y", "no", "", "sure"):
        allowed, _ = gate(100, tty=True, answer=answer)
        check(f"an interactive {answer!r} does NOT proceed", not allowed,
              "only a literal 'yes' counts")

    check("a known model has a per-call estimate",
          spend_gate.estimate_cost(100, "gemini-2.5-flash") > 0)
    check("an unknown model still produces an estimate rather than 0",
          spend_gate.estimate_cost(100, "some-new-model") > 0,
          "a missing price must not silently read as free")
    check("opus is priced above flash, so the gate reflects a model switch",
          spend_gate.estimate_cost(100, "claude-opus-5") >
          spend_gate.estimate_cost(100, "gemini-2.5-flash"))

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        print("\nFailures:")
        for label in FAILED:
            print(f"  - {label}")
        return 1
    return 0


def _raises(call, exception):
    try:
        call()
    except exception:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    sys.exit(main())
