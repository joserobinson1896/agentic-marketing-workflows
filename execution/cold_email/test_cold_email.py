"""
Tests for the cold email area.

Self-contained — no pytest, matching the repo. Run directly:

    python execution/cold_email/test_cold_email.py

No API calls, no tokens spent. What these cover is the set of things that are expensive or
impossible to discover later: copy that lies to a cold lead, a variant asserting something
false about a lead's stack, and a campaign payload Instantly rejects or — worse — accepts
and then mails as a blank body to a hundred real people.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT, SALES_FRAMEWORKS  # noqa: E402  (also puts every area on sys.path)

from build_cold_emails import (  # noqa: E402
    COLD_BANNED_PHRASES,
    COLD_FRAMEWORKS,
    MIN_PARAGRAPHS,
    build_lead,
    check_cold_rules,
    eligible_variants,
    pick_variant_for,
    normalize_typography,
    eligible_variants,
    load_variants,
    parse_stack,
    pick_variant_for,
    to_html,
)
from create_instantly_campaign import (  # noqa: E402
    DEFAULT_TIMEZONE,
    build_campaign_payload,
    build_lead_payload,
    check_rows,
)
from generate_outreach_emails import validate_draft  # noqa: E402

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not condition else ""))


def sample_row(stack="Salesforce (CRM); Stripe (Payments); Mailchimp (Email Marketing); Google Ads (Advertising)",
               employees="240"):
    return {
        "full_name": "Devon Marsh", "first_name": "Devon", "last_name": "Marsh",
        "job_title": "VP of Marketing", "email": "devon@kestrelsupply.co",
        "company_name": "Kestrel Supply Co", "company_domain": "kestrelsupply.co",
        "company_industry": "Logistics & Supply Chain", "employee_count": employees,
        "person_city": "Denver", "person_state": "CO", "phone": "+1 (303) 555-0100",
        "linkedin_url": "https://www.linkedin.com/in/devon-marsh/", "tech_stack": stack,
    }


def main():
    print("Tech stack parsing — the CSV column is the enrichment contract now")
    stack = parse_stack("Stripe (Payments); Mode (BI)")
    check("splits on semicolons", len(stack) == 2, str(stack))
    check("pulls tool and category",
          stack[0] == {"tool": "Stripe", "category": "Payments", "confidence": 1.0, "source": "lead_list"},
          str(stack[0]))
    check("blank string yields nothing", parse_stack("") == [] and parse_stack(None) == [])
    check("tolerates trailing separators", len(parse_stack("Stripe (Payments);;  ")) == 1)
    unparsed = parse_stack("SomeToolWithNoCategory")
    check("keeps an unparseable entry rather than dropping it",
          len(unparsed) == 1 and unparsed[0]["tool"] == "SomeToolWithNoCategory",
          str(unparsed))
    check("an unparseable entry still counts as a tool they run",
          unparsed[0]["category"] == "Other")
    check("tool names containing dots survive",
          parse_stack("Apollo.io (Sales Engagement)")[0]["tool"] == "Apollo.io")

    print("\nSegmentation drives which pitch a lead can legally receive")
    no_bi = build_lead(sample_row())
    check("no BI tool detected", no_bi["bi_tool"] is None, str(no_bi["bi_tool"]))
    check("segments as many_sources_no_bi", no_bi["segment"] == "many_sources_no_bi", no_bi["segment"])
    check("names at least 2 tools", len(no_bi["named_tools"]) >= 2, str(no_bi["named_tools"]))
    check("never names a tool they don't run",
          set(no_bi["named_tools"]) <= {t["tool"] for t in no_bi["tech_stack"]})

    with_bi = build_lead(sample_row(stack=sample_row()["tech_stack"] + "; Looker (BI)"))
    check("finds the BI tool", with_bi["bi_tool"] == "Looker", str(with_bi["bi_tool"]))
    check("segments as has_bi_disconnected", with_bi["segment"] == "has_bi_disconnected",
          with_bi["segment"])
    check("Sigma counts as BI (it is in this list and not in the visitor pipeline's)",
          build_lead(sample_row(stack="Stripe (Payments); Sigma (BI)"))["bi_tool"] == "Sigma")
    check("Accounting counts as a reporting source",
          "QuickBooks" in build_lead(sample_row(stack="QuickBooks (Accounting); Stripe (Payments)"))["siloed_sources"])
    check("empty stack segments as no_stack_detected",
          build_lead(sample_row(stack=""))["segment"] == "no_stack_detected")
    check("non-numeric headcount does not crash",
          build_lead(sample_row(employees="unknown"))["employee_count"] is None)

    print("\nVariant eligibility — a variant must not assert something false")
    variants = load_variants()
    ids = {v["id"] for v in variants}
    check("all six approved variants present",
          ids == {"stack_math", "monday_scramble", "blind_spot", "new_way",
                  "question_first", "ultra_short"}, str(ids))
    no_bi_pool = {v["id"] for v in eligible_variants(no_bi, variants)}
    check("blind_spot cannot rotate onto a lead with no BI tool",
          "blind_spot" not in no_bi_pool, str(no_bi_pool))
    bi_pool = {v["id"] for v in eligible_variants(with_bi, variants)}
    check("monday_scramble cannot rotate onto a BI owner",
          "monday_scramble" not in bi_pool, str(bi_pool))
    check("blind_spot is eligible for a BI owner", "blind_spot" in bi_pool)
    check("routing sends a BI owner to blind_spot",
          pick_variant_for(with_bi, variants)["id"] == "blind_spot")
    check("routing sends a tiny company to ultra_short",
          pick_variant_for(build_lead(sample_row(employees="40")), variants)["id"] == "ultra_short")
    check("eligibility never returns an empty pool",
          all(eligible_variants(lead, variants) for lead in (no_bi, with_bi)))

    print("\nCold examples obey the rules they are supposed to teach")
    for variant in variants:
        for example in variant["approved_examples"]:
            words = len(example["body"].split())
            check(f"{variant['id']}: example is 40-75 words", 40 <= words <= 75, f"{words}w")
            check(f"{variant['id']}: subject under 45 chars", len(example["subject"]) < 45,
                  f"{len(example['subject'])}ch")
            body = f"{example['subject']}\n{example['body']}".lower()
            hits = [p for p in COLD_BANNED_PHRASES if p in body]
            check(f"{variant['id']}: example uses no banned phrase", not hits, str(hits))

    print("\nWarm language is caught before it reaches a cold lead")
    lead = dict(no_bi, first_name="Devon")
    known = {t["tool"] for t in no_bi["tech_stack"]} | {"Looker", "HubSpot"}
    warm = {
        "subject": "kestrel supply",
        "body": ("Thanks for stopping by the site, Devon.\n\nWith Salesforce and Stripe "
                 "you have two answers to one question about last month's revenue here.\n\n"
                 "Can I send over a sample dashboard built on your actual stack?\n\nJose"),
    }
    problems = validate_draft(warm, lead, known, set(), banned=COLD_BANNED_PHRASES)
    check("a warm open is rejected for a cold lead",
          any("stopping by" in p for p in problems), str(problems))
    check("the same draft passes the WARM validator (so this is the cold list's doing)",
          not any("stopping by" in p for p in validate_draft(warm, lead, known, set())))

    surveil = dict(warm, body=warm["body"].replace("Thanks for stopping by the site, Devon.",
                                                   "Devon, I noticed you are using Salesforce."))
    check("explaining how we found them is rejected",
          any("i noticed" in p for p in validate_draft(surveil, lead, known, set(),
                                                       banned=COLD_BANNED_PHRASES)))
    hallucinated = dict(warm, body=warm["body"].replace("Salesforce and Stripe", "HubSpot and Looker"))
    check("naming a tool they do not run is rejected",
          any("names a tool they do not run" in p
              for p in validate_draft(hallucinated, lead, known, set(), banned=COLD_BANNED_PHRASES)))
    clean = {
        "subject": "kestrel supply's revenue picture",
        "body": ("Devon, Salesforce and Stripe each hold part of Kestrel Supply's revenue "
                 "picture and neither holds all of it, so one simple question about last "
                 "month becomes a manual reconciliation every single time.\n\nCan I send "
                 "over a sample dashboard built on your actual stack?\n\nJose"),
    }
    check("a clean cold draft passes",
          validate_draft(clean, lead, known, set(), banned=COLD_BANNED_PHRASES) == [],
          str(validate_draft(clean, lead, known, set(), banned=COLD_BANNED_PHRASES)))

    print("\nThe personalization floor counts real tools, not the curated picks")
    floor_stack = [{"tool": "HubSpot", "category": "CRM"},
                   {"tool": "Chargebee", "category": "Subscription Billing"},
                   {"tool": "Mixpanel", "category": "Product Analytics"},
                   {"tool": "Google Ads", "category": "Advertising"},
                   {"tool": "Meta Ads", "category": "Advertising"}]
    floor_lead = {"first_name": "Elias", "tech_stack": floor_stack,
                  "named_tools": ["HubSpot", "Mailchimp", "Google Ads"]}
    floor_known = {t["tool"] for t in floor_stack} | {"Mailchimp"}

    def floor_problems(body):
        return [x for x in validate_draft({"subject": "s", "body": body}, floor_lead,
                                          floor_known, set(), banned=COLD_BANNED_PHRASES)
                if "categor" in x]

    check("naming real tools that are not the curated picks passes",
          floor_problems("Elias, HubSpot, Chargebee and Mixpanel each hold a piece of the "
                         "number and none of them holds all of it for you today.") == [],
          str(floor_problems("Elias, HubSpot, Chargebee and Mixpanel each hold a piece of "
                             "the number and none of them holds all of it for you today.")))
    check("two tools from ONE category is still rejected — two ad platforms is one source",
          floor_problems("Elias, Google Ads and Meta Ads each report a number and neither "
                         "of them agrees with the other one at all here today."))
    check("a single tool is still rejected",
          floor_problems("Elias, HubSpot holds a piece of that number and it does not hold "
                         "the whole of it for you today at all here."))
    no_stack = [x for x in validate_draft(
        {"subject": "s", "body": "Elias, there is a better way to do the reporting you are "
                                 "doing by hand today than the one you are using."},
        {"first_name": "Elias", "tech_stack": [], "named_tools": []},
        floor_known, set(), banned=COLD_BANNED_PHRASES) if "categor" in x]
    check("a lead with no detected stack is exempt from the floor", no_stack == [],
          str(no_stack))

    print("\nGrounding survives vendor names that contain other vendor names")
    plus_lead = {"first_name": "Mireya",
                 "tech_stack": [{"tool": "Shopify Plus"}, {"tool": "Klaviyo"}],
                 "named_tools": ["Shopify Plus", "Klaviyo"]}
    plus_known = {"Shopify", "Shopify Plus", "Klaviyo", "HubSpot"}
    ok = {"subject": "s", "body": ("Mireya, Klaviyo and Shopify Plus each hold part of the "
                                   "revenue picture and neither of them holds the whole of "
                                   "it today.\n\nCan I send over a sample dashboard built "
                                   "on your stack?\n\nOr tell me how you pull it "
                                   "today.\n\nJose")}
    check("naming Shopify Plus is not flagged as naming Shopify",
          not any("do not run" in p for p in validate_draft(ok, plus_lead, plus_known, set())),
          str(validate_draft(ok, plus_lead, plus_known, set())))
    bare = dict(ok, body=ok["body"].replace("Shopify Plus", "Shopify"))
    check("naming bare Shopify IS still flagged, so masking didn't blunt the check",
          any("names a tool they do not run: Shopify" in p
              for p in validate_draft(bare, plus_lead, plus_known, set())))
    hallucinated = dict(ok, body=ok["body"] + "\n\nHubSpot would help here too.")
    check("an unrelated hallucinated tool is still caught",
          any("names a tool they do not run: HubSpot" in p
              for p in validate_draft(hallucinated, plus_lead, plus_known, set())))

    print("\nThe three rules the client called out: no dash, no count, one ask")
    cold_lead = {"tech_stack": [{"tool": "Google Analytics 4", "category": "Web Analytics"},
                                {"tool": "Stripe", "category": "Payments"}]}
    good = {"subject": "kestrel supply's numbers",
            "body": ("Devon,\n\nStripe and Google Analytics 4 each hold a piece of it, and "
                     "neither holds the whole thing.\n\nWe make that one number.\n\nCan I "
                     "send over a sample dashboard built on your actual stack?\n\nJose")}
    check("a compliant cold draft passes every cold rule",
          check_cold_rules(good, cold_lead) == [], str(check_cold_rules(good, cold_lead)))

    dashed = dict(good, body=good["body"].replace("Devon,", "Devon \u2014"))
    check("an em dash is rejected",
          any("em or en dash" in x for x in check_cold_rules(dashed, cold_lead)))
    check("an en dash is rejected too",
          any("em or en dash" in x for x in
              check_cold_rules(dict(good, body=good["body"].replace("Devon,", "Devon \u2013")),
                               cold_lead)))
    check("an em dash in the SUBJECT is rejected",
          any("em or en dash" in x for x in
              check_cold_rules(dict(good, subject="kestrel \u2014 numbers"), cold_lead)))

    counted = dict(good, body=good["body"].replace(
        "each hold a piece of it", "are two of nine reporting systems"))
    check("stating how many systems they run is rejected",
          any("how many systems" in x for x in check_cold_rules(counted, cold_lead)),
          str(check_cold_rules(counted, cold_lead)))
    check("'three different answers' is rejected as a count",
          any("how many systems" in x for x in check_cold_rules(
              dict(good, body=good["body"].replace(
                  "each hold a piece of it, and neither holds the whole thing",
                  "give you three different answers to one question")), cold_lead)))
    check("a numeral inside a PRODUCT name is not read as a count",
          not any("how many systems" in x for x in check_cold_rules(good, cold_lead)),
          "Google Analytics 4 must not trip the counter")
    check("'one number' as the solution is allowed",
          not any("how many systems" in x for x in check_cold_rules(good, cold_lead)))

    second_ask = dict(good, body=good["body"].replace(
        "your actual stack?\n\nJose",
        "your actual stack?\n\nOr tell me how you pull that number today.\n\nJose"))
    check("a second ask after the CTA is rejected",
          any("second ask" in x for x in check_cold_rules(second_ask, cold_lead)),
          str(check_cold_rules(second_ask, cold_lead)))
    check("the second ask is also caught by the banned phrases",
          any("or tell me" in b for b in COLD_BANNED_PHRASES))
    check("a sign-off after the CTA is fine",
          not any("second ask" in x for x in check_cold_rules(
              dict(good, body=good["body"].replace("\n\nJose", "\n\nThanks, Jose")), cold_lead)))
    check("an email with no CTA at all is rejected",
          any("no call to action" in x for x in check_cold_rules(
              dict(good, body="Devon,\n\nStripe and Google Analytics 4 both hold a piece of "
                              "it.\n\nWe fix that.\n\nJose"), cold_lead)))

    print("\nShape and typography")
    wall = {"body": "One line.\nTwo line.\nThree line.\nJose"}
    check("a wall of text is rejected",
          any("paragraph" in x for x in check_cold_rules(wall, cold_lead)))
    check("an empty body is validate_draft's problem, not this one",
          check_cold_rules({"body": ""}, cold_lead) == [])
    check("every approved example clears the paragraph floor",
          all(len([x for x in ex["body"].split("\n\n") if x.strip()]) >= MIN_PARAGRAPHS
              for v in variants for ex in v["approved_examples"]))
    check("smart apostrophes are straightened",
          normalize_typography("That\u2019s Baytree\u2019s") == "That's Baytree's")
    check("smart double quotes are straightened",
          normalize_typography("\u201chow did last month go?\u201d") == '"how did last month go?"')
    check("an em dash is swapped for a comma, so it costs no regeneration",
          normalize_typography("Pierce \u2014 Alderwood") == "Pierce, Alderwood",
          normalize_typography("Pierce \u2014 Alderwood"))
    check("the swap does not leave a doubled comma",
          normalize_typography("estimate, \u2014 and that's the number") ==
          "estimate, and that's the number",
          normalize_typography("estimate, \u2014 and that's the number"))
    check("normalizing None does not crash", normalize_typography(None) == "")

    print("\nA refusing API must stop the batch, not be retried through it")
    import build_cold_emails as bce
    calls = []

    def fake(prompt, model=None):
        calls.append(1)
        raise RuntimeError(message)

    original_call = bce.call_gemini
    bce.call_gemini = fake
    try:
        for message, fatal, label in [
            ("429 RESOURCE_EXHAUSTED. Your project has exceeded its monthly spending cap.",
             True, "a spending cap"),
            ("400 API key not valid. Please pass a valid API key.", True, "a dead key"),
            ("403 PERMISSION_DENIED", True, "permission denied"),
            ("429 RESOURCE_EXHAUSTED. Rate limit exceeded, retry shortly", False,
             "an ordinary rate limit"),
            ("503 UNAVAILABLE", False, "a 503"),
        ]:
            calls.clear()
            try:
                bce.call_gemini_with_backoff("p", retries=2)
                raised = None
            except bce.FatalGenerationError:
                raised = "fatal"
            except Exception:
                raised = "other"
            if fatal:
                check(f"{label} is fatal and is NOT retried",
                      raised == "fatal" and len(calls) == 1,
                      f"raised={raised} calls={len(calls)}")
            else:
                check(f"{label} IS retried before giving up",
                      raised == "other" and len(calls) == 2,
                      f"raised={raised} calls={len(calls)}")
    finally:
        bce.call_gemini = original_call

    check("the fatal markers cover the cap message Google actually returns",
          any(m in "your project has exceeded its monthly spending cap"
              for m in bce.FATAL_MARKERS))
    check("'quota' alone is not fatal (it appears in ordinary rate limits)",
          "quota" not in bce.FATAL_MARKERS and "quota" in bce.TRANSIENT_MARKERS)

    print("\nColleagues at one company must not get the same variant")
    def assign(sample):
        """The same rule main() applies, exercised on a handful of leads."""
        out, rot, used = [], 0, {}
        for lead in sample:
            company = (lead.get("company_name") or "").strip().lower()
            seen = used.setdefault(company, set())
            pool = eligible_variants(lead, variants)
            variant = pick_variant_for(lead, variants)
            if variant is not None and variant["id"] in seen:
                variant = None
            if variant is None:
                fresh = [v for v in pool if v["id"] not in seen] or pool
                variant = fresh[rot % len(fresh)]
                rot += 1
            seen.add(variant["id"])
            out.append(variant)
        return out

    bi_pair = [
        {"company_name": "Quinton Components", "bi_tool": "Sigma", "employee_count": 300},
        {"company_name": "Quinton Components", "bi_tool": "Sigma", "employee_count": 300},
    ]
    picks = assign(bi_pair)
    check("two BI-owning colleagues do NOT both get blind_spot",
          picks[0]["id"] != picks[1]["id"], f"{picks[0]['id']} vs {picks[1]['id']}")
    check("the first BI-owning contact still gets blind_spot (routing preserved)",
          picks[0]["id"] == "blind_spot", picks[0]["id"])
    check("the displaced colleague never gets a variant that requires a BI tool it has",
          picks[1].get("requires_bi") in (None, True))

    no_bi_pair = [
        {"company_name": "Kestrel Partners", "bi_tool": None, "employee_count": 300},
        {"company_name": "Kestrel Partners", "bi_tool": None, "employee_count": 300},
    ]
    picks = assign(no_bi_pair)
    check("two colleagues with no BI tool get different variants",
          picks[0]["id"] != picks[1]["id"])
    check("neither of them is ever handed blind_spot",
          all(p["id"] != "blind_spot" for p in picks),
          "blind_spot asserts they own a BI tool")

    solo = assign([{"company_name": "Alone Inc", "bi_tool": "Looker", "employee_count": 300}])
    check("a company with one contact still gets its routed variant",
          solo[0]["id"] == "blind_spot", solo[0]["id"])
    tiny = assign([{"company_name": "Tiny Co", "bi_tool": None, "employee_count": 40}])
    check("a small company still routes to ultra_short", tiny[0]["id"] == "ultra_short",
          tiny[0]["id"])

    print("\nUpdating leads already in Instantly")
    from update_instantly_leads import fields_that_changed, INSTANTLY_SYSTEM_FIELDS
    stored = {
        "firstName": "Pierce", "jobTitle": "Director of Growth Marketing",
        "email_subject": "old subject", "personalized_email": "old body",
        "personalized_email_html": "old body", "email_variant": "blind_spot",
        "named_tools": "Metabase (BI)",
    }
    same = {"email_subject": "old subject", "personalized_email": "old body",
            "personalized_email_html": "old body", "email_variant": "blind_spot",
            "named_tools": "Metabase (BI)", "job_title": "Director of Growth Marketing"}
    check("identical copy reports nothing changed", fields_that_changed(stored, same) == [],
          str(fields_that_changed(stored, same)))
    check("job_title is matched against Instantly's camelCased jobTitle",
          "job_title" not in fields_that_changed(stored, same))
    check("a real copy change IS reported",
          fields_that_changed(stored, dict(same, personalized_email="new body")) ==
          ["personalized_email"])
    check("a changed subject is reported",
          "email_subject" in fields_that_changed(stored, dict(same, email_subject="new")))
    check("an absent stored payload reports everything as changed",
          len(fields_that_changed({}, same)) == len(same))
    check("the copy-carrying variables are NOT treated as system fields",
          not any(k in INSTANTLY_SYSTEM_FIELDS for k in
                  ("personalized_email", "personalized_email_html", "email_subject",
                   "email_variant", "named_tools")),
          "these must round-trip verbatim or the sequence renders empty")

    from create_instantly_campaign import build_lead_payload
    row = {"email": "a@b.com", "first_name": "A", "last_name": "B", "company_name": "C",
           "company_domain": "b.com", "phone": "1", "personalized_email": "body",
           "personalized_email_html": "body<br>", "email_subject": "subj",
           "email_variant": "stack_math", "tech_stack": "Stripe (Payments)",
           "job_title": "CMO"}
    payload = build_lead_payload(row)
    check("the update sends the COMPLETE variable set, not a partial one",
          set(payload["custom_variables"]) ==
          {"personalized_email", "personalized_email_html", "email_subject",
           "email_variant", "named_tools", "job_title"},
          str(sorted(payload["custom_variables"])))
    check("every custom variable value is a scalar Instantly accepts",
          all(isinstance(v, (str, int, float, bool)) or v is None
              for v in payload["custom_variables"].values()))

    print("\nEvery approved example obeys the rules it teaches")
    for variant in variants:
        for example in variant["approved_examples"]:
            problems = check_cold_rules(example, {"tech_stack": []})
            check(f"{variant['id']}: example passes the cold rules", problems == [],
                  str(problems))

    print("\nFrameworks resolve from shared/ (both areas load the same playbook)")
    for name in COLD_FRAMEWORKS:
        check(f"{name} exists", (SALES_FRAMEWORKS / name).exists(), str(SALES_FRAMEWORKS / name))
    check("the cold delta file is loaded", "16_cold_lead.md" in COLD_FRAMEWORKS)
    check("the approved warm hybrid is still loaded", "15_warm_visitor_hybrid.md" in COLD_FRAMEWORKS)

    print("\nHTML conversion keeps the shape of the copy")
    html = to_html("Hi Devon.\n\nTwo tools.\nOne answer.\n\nJose")
    check("blank lines become paragraph breaks", html.count("<br><br>") == 2, html)
    check("single newlines become single breaks", "Two tools.<br>One answer." in html, html)
    check("escapes HTML so a & in a company name cannot break the body",
          "&amp;" in to_html("Alloy & Oak"), to_html("Alloy & Oak"))
    check("empty body yields empty string", to_html("") == "")

    print("\nInstantly campaign payload matches the v2 contract")
    payload = build_campaign_payload("Test")
    check("has the two required top-level fields",
          {"name", "campaign_schedule"} <= set(payload))
    schedule = payload["campaign_schedule"]["schedules"][0]
    check("schedule has all four required keys",
          {"name", "timing", "days", "timezone"} <= set(schedule), str(schedule.keys()))
    check("timing matches the HH:MM pattern",
          schedule["timing"] == {"from": "09:00", "to": "17:00"}, str(schedule["timing"]))
    check("timezone is Detroit, because America/New_York is not in Instantly's enum",
          DEFAULT_TIMEZONE == "America/Detroit" and "New_York" not in schedule["timezone"])
    step = payload["sequences"][0]["steps"][0]
    check("step has the three required keys", {"type", "delay", "variants"} <= set(step))
    check("step type is email", step["type"] == "email")
    check("subject is the per-lead variable", step["variants"][0]["subject"] == "{{email_subject}}")
    check("body is the per-lead variable, so no copy lives in the campaign",
          step["variants"][0]["body"] == "{{personalized_email_html}}")
    check("text-only mode swaps to the plain variable",
          build_campaign_payload("T", text_only=True)["sequences"][0]["steps"][0]["variants"][0]["body"]
          == "{{personalized_email}}")
    check("opt-out header is on (CAN-SPAM, since the copy carries no footer)",
          payload["insert_unsubscribe_header"] is True)
    check("open and link tracking are off for a cold first touch",
          payload["open_tracking"] is False and payload["link_tracking"] is False)
    check("a reply stops the whole company", payload["stop_for_company"] is True)
    check("sending accounts are omitted when not given", "email_list" not in payload)
    check("sending accounts are included when given",
          build_campaign_payload("T", ["a@b.com"])["email_list"] == ["a@b.com"])

    print("\nInstantly lead payload")
    row = dict(sample_row(), personalized_email="Hi Devon.\n\nJose",
               personalized_email_html="Hi Devon.<br><br>Jose", email_subject="a subject",
               email_variant="stack_math", email_needs_review="no")
    lead_payload = build_lead_payload(row)
    check("email is present, which campaign imports require", lead_payload["email"] == "devon@kestrelsupply.co")
    check("carries the finished body as a custom variable",
          lead_payload["custom_variables"]["personalized_email"] == "Hi Devon.\n\nJose")
    check("carries the subject as a custom variable",
          lead_payload["custom_variables"]["email_subject"] == "a subject")
    check("also fills Instantly's native personalization field",
          lead_payload["personalization"] == "Hi Devon.\n\nJose")
    check("every custom variable is a scalar — Instantly rejects objects and arrays",
          all(isinstance(v, (str, int, float, bool, type(None)))
              for v in lead_payload["custom_variables"].values()),
          str({k: type(v).__name__ for k, v in lead_payload["custom_variables"].items()}))
    check("payload is JSON-serializable", isinstance(json.dumps(lead_payload), str))

    print("\nPre-flight refuses to mail blanks")
    check("a good row passes", check_rows([row]) == [])
    check("an empty body is caught",
          any("empty personalized_email_html" in p for p in check_rows([dict(row, personalized_email_html="")])))
    check("an empty subject is caught",
          any("empty email_subject" in p for p in check_rows([dict(row, email_subject="")])))
    check("a missing email is caught",
          any("missing or malformed email" in p for p in check_rows([dict(row, email="")])))
    check("a duplicate email is caught",
          any("duplicate" in p for p in check_rows([row, dict(row)])))
    check("text-only mode checks the plain column instead",
          any("empty personalized_email" in p
              for p in check_rows([dict(row, personalized_email="")], text_only=True)))

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("\nFailures:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
