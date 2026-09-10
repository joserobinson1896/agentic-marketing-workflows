"""
Contract tests for the RB2B webhook receiver.

Self-contained — no pytest, no test framework, matching the repo (there isn't one).
Run it directly:

    python execution/visitor_identification/test_rb2b_receiver.py

These test the receiver against RB2B's documented behaviour, not against our mock.
If RB2B changes their contract, this is the file that should fail first.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)

from rb2b_site import (  # noqa: E402
    VisitorStore,
    coerce_employee_count,
    create_app,
    load_config,
    normalize_payload,
)

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not condition else ""))


def main():
    config = load_config()
    config["webhook_token"] = "test-token"
    tmp = Path(tempfile.mkdtemp(prefix="rb2b-test-"))
    store = VisitorStore(tmp / "identified.jsonl")
    app = create_app(config, store, on_pageview=None)
    client = app.test_client()
    url = "/rb2b/webhook?token=test-token"

    print("\nRB2B webhook receiver — contract tests\n")

    # --- 1. RB2B's own documented sample payload, verbatim from their docs.
    # Note their example's "Seen At" is malformed ISO ("...T12:34:56:00.00+00.00").
    print("RB2B's documented sample payload")
    sample = {
        "LinkedIn URL": "https://www.linkedin.com/in/retentionadam/",
        "First Name": "Adam",
        "Last Name": "Robinson",
        "Seen At": "2024-01-01T12:34:56:00.00+00.00",
    }
    r = client.post(url, json=sample)
    check("accepts RB2B's sample payload", r.status_code == 200, f"got {r.status_code}")
    check("reports stored", r.get_json().get("stored") is True)
    rec = store.all()[-1]
    check("normalizes to snake_case", rec["linkedin_url"] == sample["LinkedIn URL"])
    check("builds full_name", rec["full_name"] == "Adam Robinson")
    check("keeps unparseable Seen At as raw", rec["seen_at"] is None and rec["seen_at_raw"])

    # --- 2. Only LinkedIn URL + First Name are guaranteed; everything else nullable.
    print("\nNullability — only LinkedIn URL and First Name are guaranteed")
    thin = {"LinkedIn URL": "https://www.linkedin.com/in/thin-record/", "First Name": "Dana"}
    r = client.post(url, json=thin)
    check("accepts a two-field record", r.status_code == 200, f"got {r.status_code}")
    rec = store.all()[-1]
    check("nullable fields default to None", rec["business_email"] is None and rec["title"] is None)

    explicit_nulls = {
        "LinkedIn URL": "https://www.linkedin.com/in/explicit-nulls/",
        "First Name": "Rae",
        "Business Email": None,
        "Title": None,
        "Company Name": None,
        "Estimate Revenue": None,
    }
    check("accepts explicit nulls", client.post(url, json=explicit_nulls).status_code == 200)

    for missing, label in (
        ({"First Name": "NoLink"}, "missing LinkedIn URL"),
        ({"LinkedIn URL": "https://x.test/in/a"}, "missing First Name"),
    ):
        r = client.post(url, json=missing)
        check(f"rejects {label} with 400", r.status_code == 400, f"got {r.status_code}")

    # --- 3. Employee Count is typed integer OR string, and real data carries ranges.
    print("\nEmployee Count — RB2B types it integer OR string")
    cases = [(250, 250), ("250", 250), ("250+", 250), ("201-500", 201), ("11 to 50", 11),
             (None, None), ("unknown", None), ("", None)]
    for raw, expected in cases:
        got, _ = coerce_employee_count(raw)
        check(f"coerce {raw!r} -> {expected!r}", got == expected, f"got {got!r}")

    r = client.post(url, json={
        "LinkedIn URL": "https://www.linkedin.com/in/emp-string/",
        "First Name": "Sam", "Employee Count": "1,200",
    })
    rec = store.all()[-1]
    check("string Employee Count survives to the store", rec["employee_count"] == 1200,
          f"got {rec['employee_count']!r}")
    check("original Employee Count preserved", rec["employee_count_raw"] == "1,200")

    # --- 4. Auth. RB2B has no HMAC; a URL token is their documented mechanism.
    print("\nAuth — token in the query string (RB2B offers no signature verification)")
    good = {"LinkedIn URL": "https://www.linkedin.com/in/auth-test/", "First Name": "Kit"}
    check("wrong token -> 401", client.post("/rb2b/webhook?token=nope", json=good).status_code == 401)
    check("missing token -> 401", client.post("/rb2b/webhook", json=good).status_code == 401)
    check("correct token -> 200", client.post(url, json=good).status_code == 200)

    # --- 5. Dedupe on LinkedIn URL, regardless of RB2B's repeat-visitor toggle.
    print("\nDeduplication on LinkedIn URL")
    dupe = {"LinkedIn URL": "https://www.linkedin.com/in/dupe-test/", "First Name": "Lee"}
    first, second = client.post(url, json=dupe), client.post(url, json=dupe)
    check("first send stored", first.get_json()["stored"] is True)
    check("second send flagged duplicate", second.get_json()["duplicate"] is True)
    check("duplicate still acks 200", second.status_code == 200, f"got {second.status_code}")
    count = sum(1 for r in store.all() if r["linkedin_url"] == dupe["LinkedIn URL"])
    check("only one row on disk", count == 1, f"found {count}")

    # --- 6. Malformed bodies must 400, never 500. A 500 storm gets the endpoint disabled.
    print("\nMalformed bodies -> 400, never 500")
    for body, label in (("[]", "a JSON array"), ('"hello"', "a bare string"), ("not json", "garbage")):
        r = client.post(url, data=body, content_type="application/json")
        check(f"{label} -> 400", r.status_code == 400, f"got {r.status_code}")

    # --- 7. Unknown fields are preserved, not silently dropped.
    print("\nForward compatibility")
    r = client.post(url, json={
        "LinkedIn URL": "https://www.linkedin.com/in/future-field/",
        "First Name": "Ari", "Some New RB2B Field": "surprise",
    })
    rec = store.all()[-1]
    check("unknown field kept under _unmapped",
          rec["_unmapped"].get("Some New RB2B Field") == "surprise")

    # --- 8. Ack speed. RB2B disables endpoints that repeatedly time out.
    print("\nAck speed — RB2B disables slow endpoints")
    times = []
    for i in range(25):
        payload = {"LinkedIn URL": f"https://www.linkedin.com/in/speed-{i}/", "First Name": "Spd"}
        start = time.perf_counter()
        client.post(url, json=payload)
        times.append((time.perf_counter() - start) * 1000)
    worst = max(times)
    check(f"slowest ack {worst:.1f}ms is under 100ms", worst < 100, f"{worst:.1f}ms")

    # --- 9. Captured URL carries the ad attribution through untouched.
    print("\nAd attribution rides in Captured URL")
    r = client.post(url, json={
        "LinkedIn URL": "https://www.linkedin.com/in/utm-test/", "First Name": "Nico",
        "Captured URL": "http://127.0.0.1:5000/?utm_source=linkedin&utm_content=ud-a-014",
    })
    rec = store.all()[-1]
    check("utm_content preserved", "ud-a-014" in (rec["captured_url"] or ""))

    # --- 10. Website -> company_domain, which is the enrichment join key.
    print("\nDerived company_domain (the enrichment join key)")
    for site, expected in (
        ("https://northwindfreight.com", "northwindfreight.com"),
        ("http://Example.COM/pricing", "example.com"),
        (None, None),
    ):
        rec = normalize_payload({
            "LinkedIn URL": "https://x.test/in/dom", "First Name": "D", "Website": site,
        })
        check(f"{site!r} -> {expected!r}", rec["company_domain"] == expected,
              f"got {rec['company_domain']!r}")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("\nFailures:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
