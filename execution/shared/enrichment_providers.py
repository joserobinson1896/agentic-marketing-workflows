"""
Layer 3 execution tool: the enrichment provider seam.

>>> THIS IS THE FILE YOU EDIT TO GO LIVE WITH REAL TECH-STACK DATA. <<<

Every provider implements one function:

    fetch(domain, record) -> [ {"tool", "category", "confidence", "source"}, ... ]

Nothing downstream reads a provider's raw response — `enrich_visitors.py`, the email
generator and the dashboard all consume the normalized shape above. So adding ZoomInfo or
Apollo means writing one function and one category mapping, not touching the pipeline.

`normalize_stack()` does the part that actually matters and is fully testable offline:
every vendor invents its own category vocabulary ("marketing_automation", "Business
Intelligence", "email_marketing"), and our segmentation logic keys off canonical names.
See execution/visitor_identification/test_enrichment_providers.py.

WHAT EACH PROVIDER ACTUALLY GIVES YOU (researched — read before buying a seat):

  apollo      GET https://api.apollo.io/api/v1/organizations/enrich, header `x-api-key`.
              Returns `current_technologies` [{uid, name, category}] and `technology_names`.
              Best fit of the people-data vendors: it ships an explicit per-tool category,
              which is exactly our contract. Also returns firmographics in one call.
  zoominfo    Enrich Company API. Technographics over 30k+ tracked technologies, plus
              firmographics, hierarchy and funding. Deepest coverage, enterprise contract,
              JWT auth (username/password or PKI) rather than a simple API key.
  builtwith   Dedicated technographics, detected from the live site. Best signal for
              "what is actually on their pages right now" rather than "what we think they
              bought". Cheapest to start.
  clearbit    Now HubSpot Breeze Intelligence. Tech tags on the company record.
  salesnav    NOT USABLE. LinkedIn's SNAP program is closed to new partners, and the API
              does not expose technographics to third parties regardless. Sales Navigator
              has tech-stack *search filters* in its UI, but there is no API behind them.
              Registered here so asking for it returns a real explanation, not a mystery.

Only `mock` runs without credentials. The live providers are written against their
documented request shapes but are NOT exercised in this repo — no keys, and calls cost
credits. Verify against a real key before trusting a batch.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)

# Data that belongs to this area lives beside it.
HERE = Path(__file__).resolve().parent
STACKS_PATH = HERE / "tech_stacks.json"


def _load_env():
    # Absolute path, matching gemini_image_generate: a cwd-relative load_dotenv() finds
    # nothing under launchd, and then fails confusingly at auth time instead of here.
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")


# --------------------------------------------------------------- category normalization

# Our canonical categories. `data_source_categories` in tech_stacks.json decides which of
# these count as a reporting source we would unify; this list is the full vocabulary.
CANONICAL_CATEGORIES = [
    "CRM", "Payments", "Email Marketing", "Web Analytics", "Advertising", "CDP",
    "Product Analytics", "Ecommerce", "Data Warehouse", "BI", "Subscription Billing",
    "Sales Engagement", "Support", "ERP", "Accounting",
]

# Vendor vocabulary -> ours. Keys are compared lowercased with separators collapsed, so
# "Marketing Automation", "marketing_automation" and "marketing-automation" all hit.
CATEGORY_ALIASES = {
    "crm": "CRM",
    "customer relationship management": "CRM",
    "sales crm": "CRM",
    "marketing automation": "Email Marketing",
    "email marketing": "Email Marketing",
    "email": "Email Marketing",
    "esp": "Email Marketing",
    "transactional email": "Email Marketing",
    "analytics": "Web Analytics",
    "web analytics": "Web Analytics",
    "site analytics": "Web Analytics",
    "product analytics": "Product Analytics",
    "mobile analytics": "Product Analytics",
    "advertising": "Advertising",
    "advertising networks": "Advertising",
    "ad network": "Advertising",
    "ppc": "Advertising",
    "retargeting": "Advertising",
    "conversion tracking": "Advertising",
    "payments": "Payments",
    "payment processing": "Payments",
    "payment processors": "Payments",
    "payment": "Payments",
    "ecommerce": "Ecommerce",
    "e commerce": "Ecommerce",
    "ecommerce platforms": "Ecommerce",
    "shopping cart": "Ecommerce",
    "cdp": "CDP",
    "customer data platform": "CDP",
    "tag management": "CDP",
    "data warehouse": "Data Warehouse",
    "warehouse": "Data Warehouse",
    "data warehousing": "Data Warehouse",
    "bi": "BI",
    "business intelligence": "BI",
    "data visualization": "BI",
    "reporting": "BI",
    "dashboards": "BI",
    "subscription billing": "Subscription Billing",
    "recurring billing": "Subscription Billing",
    "subscriptions": "Subscription Billing",
    "sales engagement": "Sales Engagement",
    "sales automation": "Sales Engagement",
    "sales enablement": "Sales Engagement",
    "support": "Support",
    "customer support": "Support",
    "help desk": "Support",
    "helpdesk": "Support",
    "live chat": "Support",
    "ticketing": "Support",
    "erp": "ERP",
    "enterprise resource planning": "ERP",
    "accounting": "Accounting",
    "bookkeeping": "Accounting",
    "invoicing": "Accounting",
}

# Last resort when a vendor gives no category at all, or one we don't recognize: infer
# from the tool name. Keeps a known tool from falling out of `siloed_sources` — which is
# what the whole pitch is counted from — just because a vendor renamed a category.
TOOL_CATEGORY_FALLBACK = {
    "hubspot": "CRM", "salesforce": "CRM", "pipedrive": "CRM", "zoho crm": "CRM",
    "microsoft dynamics": "CRM", "close": "CRM",
    "stripe": "Payments", "braintree": "Payments", "adyen": "Payments",
    "paypal": "Payments", "square": "Payments",
    "mailchimp": "Email Marketing", "klaviyo": "Email Marketing", "marketo": "Email Marketing",
    "pardot": "Email Marketing", "customer.io": "Email Marketing", "braze": "Email Marketing",
    "sendgrid": "Email Marketing", "iterable": "Email Marketing", "activecampaign": "Email Marketing",
    "google analytics": "Web Analytics", "google analytics 4": "Web Analytics",
    "adobe analytics": "Web Analytics", "plausible": "Web Analytics", "fathom": "Web Analytics",
    "mixpanel": "Product Analytics", "amplitude": "Product Analytics", "heap": "Product Analytics",
    "posthog": "Product Analytics", "pendo": "Product Analytics",
    "segment": "CDP", "rudderstack": "CDP", "mparticle": "CDP", "google tag manager": "CDP",
    "shopify": "Ecommerce", "bigcommerce": "Ecommerce", "woocommerce": "Ecommerce",
    "magento": "Ecommerce",
    "snowflake": "Data Warehouse", "bigquery": "Data Warehouse", "redshift": "Data Warehouse",
    "databricks": "Data Warehouse",
    "looker": "BI", "tableau": "BI", "power bi": "BI", "mode": "BI", "metabase": "BI",
    "sigma": "BI", "domo": "BI",
    "recharge": "Subscription Billing", "chargebee": "Subscription Billing",
    "recurly": "Subscription Billing", "zuora": "Subscription Billing",
    "outreach": "Sales Engagement", "salesloft": "Sales Engagement", "apollo": "Sales Engagement",
    "zendesk": "Support", "intercom": "Support", "gorgias": "Support", "freshdesk": "Support",
    "netsuite": "ERP", "sap": "ERP",
    "quickbooks": "Accounting", "xero": "Accounting",
    "google ads": "Advertising", "meta ads": "Advertising", "facebook ads": "Advertising",
    "linkedin ads": "Advertising", "tiktok ads": "Advertising", "bing ads": "Advertising",
}


def canonical_category(raw, tool_name=None):
    """Map a vendor's category string onto ours, falling back to the tool name."""
    if raw:
        key = " ".join(str(raw).replace("_", " ").replace("-", " ").lower().split())
        if key in CATEGORY_ALIASES:
            return CATEGORY_ALIASES[key]
        for canon in CANONICAL_CATEGORIES:
            if key == canon.lower():
                return canon
    if tool_name:
        name = str(tool_name).strip().lower()
        if name in TOOL_CATEGORY_FALLBACK:
            return TOOL_CATEGORY_FALLBACK[name]
        for known, category in TOOL_CATEGORY_FALLBACK.items():
            if known in name:
                return category
    return "Other"


def normalize_stack(entries, source_label="api", default_confidence=0.75):
    """Coerce any provider's per-tool records into our contract.

    Accepts {tool|name}, {category}, {confidence}, {source} in any combination, drops
    unnamed rows, canonicalizes categories, and de-duplicates on tool name keeping the
    highest confidence.
    """
    out = {}
    for entry in entries or []:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            continue
        name = (entry.get("tool") or entry.get("name") or "").strip()
        if not name:
            continue
        try:
            confidence = float(entry.get("confidence", default_confidence))
        except (TypeError, ValueError):
            confidence = default_confidence
        item = {
            "tool": name,
            "category": canonical_category(entry.get("category"), name),
            "confidence": round(max(0.0, min(1.0, confidence)), 2),
            "source": entry.get("source") or source_label,
        }
        existing = out.get(name.lower())
        if existing is None or item["confidence"] > existing["confidence"]:
            out[name.lower()] = item
    return sorted(out.values(), key=lambda t: (-t["confidence"], t["tool"]))


# ------------------------------------------------------------------------- providers


def fetch_mock(domain, record=None):
    """Fixture-backed. The only provider that runs with no credentials."""
    if not domain:
        return []
    stacks = json.loads(STACKS_PATH.read_text())["stacks"]
    return normalize_stack(stacks.get(domain.lower(), []), source_label="mock")


def fetch_apollo(domain, record=None):
    """Apollo.io organization enrichment.

    GET https://api.apollo.io/api/v1/organizations/enrich  header: x-api-key
    One of domain / linkedin_url / website is required; passing more improves the match.
    Response carries `current_technologies` [{uid, name, category}] and `technology_names`.

    UNVERIFIED: written from Apollo's documented shape, never run against a live key here.
    """
    import requests

    _load_env()
    api_key = os.environ.get("APOLLO_API_KEY")
    if not api_key:
        raise RuntimeError("APOLLO_API_KEY not set in .env")

    params = {"domain": domain}
    if record and record.get("linkedin_url"):
        params["linkedin_url"] = record["linkedin_url"]

    resp = requests.get(
        "https://api.apollo.io/api/v1/organizations/enrich",
        params=params,
        headers={"x-api-key": api_key, "Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    org = (resp.json() or {}).get("organization") or {}

    entries = org.get("current_technologies") or []
    if not entries:
        # Fall back to the flat name list; canonical_category infers from the tool name.
        entries = [{"name": n} for n in (org.get("technology_names") or [])]
    return normalize_stack(entries, source_label="apollo")


def fetch_zoominfo(domain, record=None):
    """ZoomInfo Enrich Company API (technographics across 30k+ tracked technologies).

    Auth is a JWT from their authenticate endpoint (username/password or PKI), not a
    static key, so a real implementation needs a token cache — ZoomInfo rate-limits
    authentication separately from enrichment.

    Map their technology rows through normalize_stack() and everything downstream works.
    """
    raise NotImplementedError(
        "ZoomInfo is not wired up. Implement the JWT auth exchange, call Enrich Company "
        "with the domain, then return normalize_stack(<their technology rows>, "
        "source_label='zoominfo'). Nothing else in the pipeline needs to change."
    )


def fetch_builtwith(domain, record=None):
    """BuiltWith — technographics detected from the live site.

    GET https://api.builtwith.com/v21/api.json?KEY=<key>&LOOKUP=<domain>
    Results nest as Results[0].Result.Paths[].Technologies[] with Name and Categories.

    UNVERIFIED: written from BuiltWith's documented shape, never run against a live key.
    """
    import requests

    _load_env()
    api_key = os.environ.get("BUILTWITH_API_KEY")
    if not api_key:
        raise RuntimeError("BUILTWITH_API_KEY not set in .env")

    resp = requests.get(
        "https://api.builtwith.com/v21/api.json",
        params={"KEY": api_key, "LOOKUP": domain},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json() or {}

    entries = []
    for result in payload.get("Results") or []:
        for path in (result.get("Result") or {}).get("Paths") or []:
            for tech in path.get("Technologies") or []:
                categories = tech.get("Categories") or []
                entries.append({
                    "name": tech.get("Name"),
                    "category": categories[0] if categories else None,
                    "source": "builtwith detection",
                })
    return normalize_stack(entries, source_label="builtwith")


def fetch_clearbit(domain, record=None):
    """Clearbit / HubSpot Breeze Intelligence company enrichment.

    GET https://company.clearbit.com/v2/companies/find?domain=<domain>
    Bearer auth. Tech signals arrive as `tech` (slugs) and `techCategories`.

    UNVERIFIED: shape only. Clearbit's tech list is slugs without per-tool categories, so
    canonical_category() infers from the tool name via TOOL_CATEGORY_FALLBACK.
    """
    import requests

    _load_env()
    api_key = os.environ.get("CLEARBIT_API_KEY")
    if not api_key:
        raise RuntimeError("CLEARBIT_API_KEY not set in .env")

    resp = requests.get(
        "https://company.clearbit.com/v2/companies/find",
        params={"domain": domain},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=20,
    )
    resp.raise_for_status()
    company = resp.json() or {}
    entries = [{"name": slug.replace("_", " ").title()} for slug in (company.get("tech") or [])]
    return normalize_stack(entries, source_label="clearbit")


def fetch_salesnav(domain, record=None):
    """LinkedIn Sales Navigator — deliberately unavailable, with the reason.

    Sales Navigator shows tech-stack filters in its UI, so it looks like the obvious
    source. It isn't: LinkedIn's SNAP partner program is closed to new partners (no form,
    no waitlist, no published timeline), and even for existing partners the API does not
    expose technographics to third parties.

    Use Apollo or BuiltWith for the stack. Sales Navigator stays a manual research tool.
    """
    raise NotImplementedError(
        "LinkedIn Sales Navigator cannot supply tech-stack data via API: the SNAP program "
        "is closed to new partners and does not expose technographics to third parties. "
        "Use --provider apollo or --provider builtwith instead."
    )


PROVIDERS = {
    "mock": fetch_mock,
    "apollo": fetch_apollo,
    "zoominfo": fetch_zoominfo,
    "builtwith": fetch_builtwith,
    "clearbit": fetch_clearbit,
    "salesnav": fetch_salesnav,
}

# Providers that need no credentials and are safe to run in CI or a demo.
OFFLINE_PROVIDERS = {"mock"}


def get_provider(name):
    fetch = PROVIDERS.get(name)
    if fetch is None:
        raise ValueError(f"unknown provider: {name} (have: {', '.join(sorted(PROVIDERS))})")
    return fetch
