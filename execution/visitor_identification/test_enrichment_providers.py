"""
Tests for the enrichment provider seam.

Self-contained — no pytest, matching the repo. Run directly:

    python execution/visitor_identification/test_enrichment_providers.py

These exercise the part of the seam that decides whether a real provider's data survives
into the pitch: category normalization. Every vendor invents its own vocabulary
("marketing_automation", "Business Intelligence", bare slugs with no category at all), and
our segmentation counts `siloed_sources` by canonical category. A vendor category we fail
to map silently drops a tool out of the pitch — which is why this is tested against real
response shapes rather than left to be discovered on a live key.

Live API calls are NOT made. Fixtures below mirror each vendor's documented response shape.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)

from enrichment_providers import (  # noqa: E402
    OFFLINE_PROVIDERS,
    PROVIDERS,
    canonical_category,
    fetch_mock,
    get_provider,
    normalize_stack,
)

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not condition else ""))


def main():
    print("\nEnrichment provider seam\n")

    # --- 1. Vendor category vocabulary -> ours.
    print("Category normalization across vendor vocabularies")
    cases = [
        ("crm", "CRM"),                              # Apollo slug
        ("Customer Relationship Management", "CRM"), # ZoomInfo prose
        ("marketing_automation", "Email Marketing"), # Apollo
        ("Email Marketing", "Email Marketing"),      # BuiltWith
        ("analytics", "Web Analytics"),
        ("Business Intelligence", "BI"),
        ("data-visualization", "BI"),                # hyphenated
        ("Payment Processors", "Payments"),          # BuiltWith plural
        ("Ecommerce Platforms", "Ecommerce"),
        ("advertising networks", "Advertising"),
        ("recurring_billing", "Subscription Billing"),
        ("help desk", "Support"),
        ("customer data platform", "CDP"),
    ]
    for raw, expected in cases:
        got = canonical_category(raw)
        check(f"{raw!r} -> {expected!r}", got == expected, f"got {got!r}")

    # --- 2. No category at all (Clearbit ships bare slugs) -> infer from the tool name.
    print("\nCategory inference when the vendor gives none")
    for tool, expected in [
        ("HubSpot", "CRM"), ("Stripe", "Payments"), ("Klaviyo", "Email Marketing"),
        ("Google Analytics 4", "Web Analytics"), ("Looker", "BI"), ("Shopify", "Ecommerce"),
        ("Snowflake", "Data Warehouse"), ("Segment", "CDP"), ("Recharge", "Subscription Billing"),
    ]:
        got = canonical_category(None, tool)
        check(f"{tool} -> {expected}", got == expected, f"got {got!r}")

    check("unknown tool with no category -> Other", canonical_category(None, "Wingbat 9000") == "Other")
    check("unrecognized category falls back to the tool name",
          canonical_category("Fancy New Bucket", "HubSpot") == "CRM")

    # --- 3. Apollo's documented shape: current_technologies [{uid, name, category}].
    print("\nApollo response shape")
    apollo = [
        {"uid": "hubspot", "name": "HubSpot", "category": "crm"},
        {"uid": "stripe", "name": "Stripe", "category": "payments"},
        {"uid": "marketo", "name": "Marketo", "category": "marketing_automation"},
        {"uid": "ga", "name": "Google Analytics 4", "category": "analytics"},
    ]
    stack = normalize_stack(apollo, source_label="apollo")
    check("all 4 tools survive", len(stack) == 4, f"got {len(stack)}")
    cats = {t["tool"]: t["category"] for t in stack}
    check("HubSpot -> CRM", cats.get("HubSpot") == "CRM")
    check("Marketo -> Email Marketing", cats.get("Marketo") == "Email Marketing")
    check("source label applied", all(t["source"] == "apollo" for t in stack))
    check("default confidence applied", all(0 < t["confidence"] <= 1 for t in stack))

    # --- 4. Apollo's flat fallback list: technology_names, strings only.
    print("\nApollo technology_names fallback (bare strings)")
    stack = normalize_stack(["HubSpot", "Stripe", "Mailchimp"], source_label="apollo")
    check("plain strings accepted", len(stack) == 3, f"got {len(stack)}")
    check("categories inferred from names",
          {t["category"] for t in stack} == {"CRM", "Payments", "Email Marketing"},
          str({t["category"] for t in stack}))

    # --- 5. BuiltWith's shape: Name + Categories[].
    print("\nBuiltWith response shape")
    builtwith = [
        {"name": "Shopify", "category": "Ecommerce Platforms", "source": "builtwith detection"},
        {"name": "Klaviyo", "category": "Email Marketing", "source": "builtwith detection"},
    ]
    stack = normalize_stack(builtwith, source_label="builtwith")
    check("Shopify -> Ecommerce", stack[0]["category"] in {"Ecommerce", "Email Marketing"})
    check("per-entry source preserved over the label",
          all(t["source"] == "builtwith detection" for t in stack))

    # --- 6. Junk in, sane out. A provider hiccup must not crash a batch.
    print("\nMalformed provider rows")
    messy = [
        {"name": "HubSpot", "confidence": "not a number"},
        {"name": ""}, {}, None, "Stripe", {"name": "  Klaviyo  "},
        {"name": "HubSpot", "confidence": 5.0},     # out of range, and a duplicate
    ]
    stack = normalize_stack(messy)
    names = {t["tool"] for t in stack}
    check("empty/None/garbage rows dropped", names == {"HubSpot", "Stripe", "Klaviyo"}, str(names))
    check("whitespace stripped", "Klaviyo" in names)
    check("confidence clamped to <= 1.0", all(t["confidence"] <= 1.0 for t in stack))
    check("duplicate tool collapsed to one row",
          sum(1 for t in stack if t["tool"] == "HubSpot") == 1)
    check("bad confidence didn't raise", True)

    # --- 7. Sorted by confidence so pick_named_tools() gets a stable best-first list.
    print("\nOrdering")
    stack = normalize_stack([
        {"name": "Low", "category": "crm", "confidence": 0.3},
        {"name": "High", "category": "crm", "confidence": 0.99},
        {"name": "Mid", "category": "crm", "confidence": 0.6},
    ])
    check("highest confidence first", [t["tool"] for t in stack] == ["High", "Mid", "Low"],
          str([t["tool"] for t in stack]))

    # --- 8. The registry itself.
    print("\nProvider registry")
    for name in ["mock", "apollo", "zoominfo", "builtwith", "clearbit", "salesnav"]:
        check(f"{name} registered", name in PROVIDERS)
    check("only mock runs without credentials", OFFLINE_PROVIDERS == {"mock"})

    try:
        get_provider("nope")
        check("unknown provider raises", False)
    except ValueError as exc:
        check("unknown provider raises ValueError naming the options", "apollo" in str(exc))

    # Sales Navigator must explain itself rather than fail mysteriously — it's the one
    # people ask for and the one that genuinely cannot work.
    try:
        get_provider("salesnav")("example.com", None)
        check("salesnav refuses", False)
    except NotImplementedError as exc:
        check("salesnav explains why LinkedIn can't do this", "SNAP" in str(exc))
        check("salesnav points at a working alternative", "apollo" in str(exc).lower())

    try:
        get_provider("zoominfo")("example.com", None)
        check("zoominfo stub refuses", False)
    except NotImplementedError as exc:
        check("zoominfo stub names the missing work", "JWT" in str(exc))

    # --- 9. The mock still satisfies the same contract as everything else.
    print("\nMock provider conforms to the shared contract")
    stack = fetch_mock("northwindfreight.com")
    check("returns rows", len(stack) == 6, f"got {len(stack)}")
    check("every row has the 4 contract keys",
          all({"tool", "category", "confidence", "source"} <= set(t) for t in stack))
    check("categories are canonical",
          {t["category"] for t in stack} <= {
              "CRM", "Payments", "Email Marketing", "Web Analytics", "Advertising",
              "CDP", "Product Analytics", "Ecommerce", "Data Warehouse", "BI",
              "Subscription Billing", "Sales Engagement", "Support", "ERP", "Accounting", "Other"},
          str({t["category"] for t in stack}))
    check("unknown domain returns empty, not an error", fetch_mock("nobody.example") == [])
    check("None domain returns empty", fetch_mock(None) == [])

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("\nFailures:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
