#!/usr/bin/env python3
"""Generate five mock platform CSVs for the Unified Dashboards funnel.

Stands in for the tools a real business runs so a dashboard can be built without
touching anyone's live Stripe or CRM. Everything here is fictional.

The five files are designed as puzzle pieces, not five unrelated tables. They
share keys that join cleanly (campaign_id, session_id, lead_id,
stripe_customer_id) and they encode ONE causal story:

    Meta buys the cheapest leads and the worst customers.

    Meta finds small companies with few siloed tools -> those leads book demos
    further out -> further-out bookings no-show more -> the ones who close buy
    Starter -> Starter accounts connect 3 sources, get little value, and churn
    by month 4. LinkedIn and RB2B cost far more per lead and find companies with
    real silo pain, who close on Growth and stay.

    So the channel ranking INVERTS as you walk down the funnel:
    cost per lead -> cost per demo held -> cost per closed-won -> cost per
    dollar still being billed in month 6.

The hinge column is `siloed_source_count` on crm_deals. It is drawn per channel
and then drives tier, close rate, expansion and churn hazard. Nothing downstream
is random-for-its-own-sake; if you change a channel's source distribution here,
its whole funnel moves.

Two more findings are planted for a dashboard to surface:
  * Booking lead time drives no-shows (>=6 days out ~34% no-show, <=2 days ~8%).
  * Showing the sample dashboard on the call roughly doubles close rate, and
    reps vary a lot in how often they do it -- coachable, and it maps to the
    fixed CTA in shared/sales_frameworks/.

Deterministic: one seed, byte-identical output on every run.

    python execution/mock_data/generate_platform_data.py
"""

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path
from random import Random

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every area on sys.path)

SEED = 20260908
WINDOW_START = date(2025, 3, 1)
WINDOW_END = date(2026, 8, 31)

# Canon pricing from shared/sales_frameworks/30_offer.md. Do not invent tiers.
TIERS = {
    "Starter": {"mrr": 2400, "max_sources": 4},
    "Growth": {"mrr": 4800, "max_sources": 10},
    "Enterprise": {"mrr": 9500, "max_sources": 12},
}

# ---------------------------------------------------------------------------
# Channel model -- this dict IS the story. Every downstream number derives here.
#
#   leads       how many leads the channel produced over the 8-month window
#   cpl         target cost per lead; channel budget = cpl * leads, so the
#               spend file ties out to exactly this number by construction
#   sources     triangular (low, high, mode) for siloed_source_count -- the
#               hinge that drives tier, close rate and churn
#   book        P(lead books a first meeting)
#   close       base P(close | meeting held), before the sample-dashboard and
#               source-count multipliers
#   lead_time   triangular (low, high, mode) days between booking and meeting;
#               longer means more no-shows
#   emp         triangular employee-count range for the company
# ---------------------------------------------------------------------------
CHANNELS = {
    "meta_ads": dict(
        leads=420, cpl=190.0, sources=(2, 5, 2.8), book=0.42, close=0.175,
        lead_time=(2, 18, 8.5), emp=(25, 180, 70), advance=0.45,
    ),
    "google_ads": dict(
        leads=130, cpl=460.0, sources=(3, 9, 5.8), book=0.58, close=0.29,
        lead_time=(1, 11, 4.2), emp=(60, 420, 190), advance=0.62,
    ),
    "linkedin_ads": dict(
        leads=78, cpl=1240.0, sources=(6, 12, 8.6), book=0.68, close=0.42,
        lead_time=(0, 7, 2.6), emp=(180, 900, 400), advance=0.74,
    ),
    "cold_email": dict(
        leads=120, cpl=380.0, sources=(5, 11, 7.4), book=0.40, close=0.25,
        lead_time=(1, 11, 4.4), emp=(120, 640, 300), advance=0.58,
    ),
    "rb2b_visitor_id": dict(
        leads=100, cpl=280.0, sources=(6, 12, 8.4), book=0.62, close=0.45,
        lead_time=(0, 8, 3.1), emp=(150, 780, 340), advance=0.72,
    ),
    "organic": dict(
        leads=55, cpl=0.0, sources=(3, 10, 6.2), book=0.50, close=0.26,
        lead_time=(1, 12, 4.5), emp=(50, 500, 210), advance=0.60,
    ),
}

# Campaigns per channel: (campaign_id, campaign_name, objective, audience,
# flight start, flight end, share of the channel's budget).
CAMPAIGNS = [
    ("meta_ads", "cmp_meta_saas_founders", "Prospecting | SaaS founders",
     "conversions", "SaaS founders, US, 1%LAL", WINDOW_START, WINDOW_END, 0.34),
    ("meta_ads", "cmp_meta_ecommerce", "Prospecting | Ecommerce operators",
     "conversions", "Ecommerce ops, US, interest stack", WINDOW_START, date(2026, 3, 31), 0.22),
    ("meta_ads", "cmp_meta_agencies", "Prospecting | Marketing agencies",
     "conversions", "Agency owners, US", date(2025, 8, 1), WINDOW_END, 0.26),
    ("meta_ads", "cmp_meta_retargeting", "Retargeting | Site visitors 30d",
     "conversions", "Website visitors 30d", date(2025, 10, 1), WINDOW_END, 0.18),
    ("google_ads", "cmp_google_dashboard_terms", "Search | Reporting dashboard terms",
     "search", "Exact + phrase, reporting dashboard", WINDOW_START, WINDOW_END, 0.46),
    ("google_ads", "cmp_google_competitor", "Search | Competitor terms",
     "search", "Competitor brand terms", date(2025, 6, 1), WINDOW_END, 0.30),
    ("google_ads", "cmp_google_pmax", "PMax | Revenue reporting",
     "performance_max", "Broad, revenue reporting signals", date(2025, 11, 1), WINDOW_END, 0.24),
    ("linkedin_ads", "cmp_li_abm_revops", "ABM | RevOps and VP Marketing",
     "lead_gen", "RevOps + VP Marketing, 200-1000 emp", date(2025, 4, 1), WINDOW_END, 0.58),
    ("linkedin_ads", "cmp_li_leadgen_cmo", "Lead gen form | CMO and Head of Growth",
     "lead_gen", "CMO + Head of Growth, US", date(2025, 9, 1), WINDOW_END, 0.42),
    ("cold_email", "cmp_instantly_saas_ops", "Instantly | SaaS + ops sequence",
     "cold_email", "Analytics + RevOps titles, 201-500", date(2025, 3, 10), date(2025, 12, 31), 0.55),
    ("cold_email", "cmp_instantly_logistics", "Instantly | Logistics and services",
     "cold_email", "Ops leaders, logistics + prof services", date(2025, 11, 1), WINDOW_END, 0.45),
    ("rb2b_visitor_id", "cmp_rb2b_site", "RB2B | Site visitor identification",
     "identification", "Anonymous US site traffic", WINDOW_START, WINDOW_END, 1.0),
]

PLATFORM_OF = {
    "meta_ads": "Meta Ads",
    "google_ads": "Google Ads",
    "linkedin_ads": "LinkedIn Ads",
    "cold_email": "Instantly",
    "rb2b_visitor_id": "RB2B",
}

# Five reps. show_sample is the whole point: it varies wildly and it moves the
# close rate more than anything the rep says.
REPS = [
    {"name": "Jordan Alvarez", "show_sample": 0.98},
    {"name": "Sasha Bell", "show_sample": 0.82},
    {"name": "Nina Okafor", "show_sample": 0.70},
    {"name": "Rory Feld", "show_sample": 0.50},
    {"name": "Chris Lindgren", "show_sample": 0.32},
]

BI_TOOLS = ["Looker", "Tableau", "Power BI", "Mode", "Metabase"]

INDUSTRIES = [
    "B2B SaaS", "Professional Services", "Manufacturing", "Healthcare Technology",
    "Logistics & Supply Chain", "Financial Services", "Ecommerce & DTC",
    "Marketing Agency", "Cybersecurity", "Real Estate", "Consumer Goods",
    "Wholesale & Distribution",
]

TITLES = {
    "c_suite": ["Chief Marketing Officer", "Chief Operating Officer",
                "Chief Revenue Officer", "Chief Commercial Officer", "Founder"],
    "vp": ["VP Marketing", "VP Operations", "VP Revenue Operations", "VP Sales",
           "VP Growth"],
    "director": ["Director of Marketing", "Director of Revenue Operations",
                 "Director of Analytics", "Director of Demand Generation",
                 "Director of Sales Enablement"],
    "manager": ["Marketing Operations Manager", "Analytics Manager",
                "Demand Generation Manager", "Growth Marketing Manager",
                "Revenue Operations Manager"],
}

CITIES = [
    ("Chicago", "IL"), ("Austin", "TX"), ("Boston", "MA"), ("Denver", "CO"),
    ("Seattle", "WA"), ("Atlanta", "GA"), ("Minneapolis", "MN"), ("Portland", "OR"),
    ("Tampa", "FL"), ("Charleston", "SC"), ("Raleigh", "NC"), ("Phoenix", "AZ"),
    ("Nashville", "TN"), ("Columbus", "OH"), ("Salt Lake City", "UT"),
    ("Kansas City", "MO"), ("Philadelphia", "PA"), ("San Diego", "CA"),
]

FIRST_NAMES = [
    "Dana", "Marcus", "Priya", "Tobias", "Alina", "Ray", "Simone", "Nate",
    "Camille", "Devon", "Pierce", "Lena", "Bianca", "Omar", "Greta", "Julian",
    "Nadia", "Colin", "Rosa", "Elliot", "Maya", "Theo", "Ingrid", "Desmond",
    "Frida", "Hugo", "Willa", "Otto", "Sylvie", "Bennett", "Corinne", "Malik",
    "Tessa", "Gideon", "Noor", "Vance", "Delia", "Rafael", "Imogen", "Casper",
]

LAST_NAMES = [
    "Whitfield", "Ortega", "Raman", "Kerr", "Moreau", "Calloway", "Achebe",
    "Kowalski", "Ferrar", "Pryce", "Ibarra", "Cortland", "Vandermeer", "Sato",
    "Blackwood", "Nunez", "Halloran", "Reyes", "Thackeray", "Okonjo", "Bright",
    "Salvatore", "Lindqvist", "Ashford", "Barrow", "Cheng", "Delacroix",
    "Ellery", "Fontaine", "Greaves", "Hartnell", "Ivanov", "Jessup", "Kingsley",
    "Lowry", "Mercer", "Novak", "Ophelia", "Pemberton", "Quintero",
]

COMPANY_HEADS = [
    "Alderwood", "Amberline", "Ashcombe", "Ashgrove", "Baytree", "Bellweather",
    "Brackenridge", "Bramblewood", "Brightline", "Cadence", "Caldera",
    "Clearfield", "Copperline", "Corvid", "Dovetail", "Drayton", "Dunmore",
    "Duskwater", "Eastgate", "Elmridge", "Northwind", "Harborlight", "Tidewater",
    "Kestrel", "Vaultpoint", "Ledgerline", "Brightseed", "Sunbelt", "Marlow",
    "Fernbank", "Glenmoor", "Hartfield", "Ironwood", "Junegrass", "Kingsmoor",
    "Larkspur", "Mossvale", "Nightjar", "Oakhurst", "Pinehollow", "Quarrystone",
    "Redstone", "Silverbirch", "Thornbury", "Umberfield", "Vantage", "Westmere",
    "Yarrow", "Zephyr", "Cobalt", "Meridian", "Bluecrest", "Foxglove",
    "Highmark", "Stonebridge", "Winterbourne", "Ravenscroft", "Applegate",
    "Birchwood", "Coldstream",
]

COMPANY_TAILS = [
    "Collective", "Cloud", "Partners", "Capital", "Logistics", "Systems",
    "Labs", "Group", "Supply Co", "Industries", "Analytics", "Health",
    "Provisions", "Transport", "Advisors", "Consulting", "Mercantile",
    "Outfitters", "Care Systems", "Technologies", "Works", "Networks",
    "Digital", "Commerce", "Financial", "Robotics", "Interactive", "Studios",
    "Brands", "Media",
]

LANDING_PAGES = [
    "/", "/pricing", "/how-it-works", "/sample-dashboard",
    "/lp/saas-founders", "/lp/ecommerce", "/lp/agencies",
    "/blog/six-dashboards-a-day", "/blog/four-tools-four-truths",
]

LOST_REASONS = [
    "No budget this quarter", "Not enough data sources to justify it",
    "Building it in-house", "Went dark after the walkthrough",
    "Timing, revisit next quarter", "Chose a self-serve BI tool",
    "No show, unresponsive", "Wanted software, not a service",
]

OBJECTIONS = [
    "Price", "Wants self-serve software", "Already has a BI tool",
    "Needs IT signoff", "Data privacy review", "Not enough sources",
    "Internal analyst can do it", "",
]

CHURN_REASONS = [
    "Not enough sources to justify cost", "Built it in-house", "Budget cut",
    "Champion left the company", "Consolidated into their BI tool",
    "Never fully onboarded",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def days_between(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def tri_int(rng, spec, lo_clamp=None, hi_clamp=None):
    """Rounded draw from a triangular (low, high, mode) spec."""
    low, high, mode = spec
    value = int(round(rng.triangular(low, high, mode)))
    if lo_clamp is not None:
        value = max(lo_clamp, value)
    if hi_clamp is not None:
        value = min(hi_clamp, value)
    return value


def weekday_weight(day):
    """Ad delivery and B2B traffic both sag at the weekend."""
    return 0.42 if day.weekday() >= 5 else 1.0


def stamp(day, rng, lo=8, hi=19):
    """A plausible business-hours timestamp on `day`, ISO-8601."""
    return "%sT%02d:%02d:%02d" % (day.isoformat(), rng.randint(lo, hi),
                                  rng.randint(0, 59), rng.randint(0, 59))


def tier_for_sources(sources, rng):
    """The hinge: how many silos you have decides what you can justify buying."""
    if sources <= 4:
        return "Starter" if rng.random() < 0.88 else "Growth"
    if sources <= 8:
        return "Growth" if rng.random() < 0.86 else "Starter"
    return "Enterprise" if rng.random() < 0.34 else "Growth"


def subscription_lifetime(rng, connected_sources):
    """Months of billing before the account cancels.

    Value is proportional to how many silos we unify, so lifetime rises with
    connected sources. This one curve produces the entire retention story.

    Drawing a lifetime up front rather than rolling a monthly churn hazard is
    deliberate: a hazard of 0.15 vs 0.015 is a big gap in expectation but on the
    7-20 subscription cohorts a single channel produces in a year it disappears
    into binomial noise, and the ranking came out backwards run to run. Real
    churn clusters by tenure anyway, so a tight lifetime distribution is both
    more legible and more honest than a memoryless coin flip.
    """
    if connected_sources <= 3:
        spec = (1, 10, 3.5)
    elif connected_sources <= 5:
        spec = (2, 12, 5.0)
    elif connected_sources <= 7:
        spec = (3, 16, 7.5)
    elif connected_sources <= 9:
        spec = (6, 30, 18.0)
    else:
        spec = (9, 36, 24.0)
    return max(1, int(round(rng.triangular(*spec))))


def show_probability(lead_time_days):
    """Booked far out means forgotten. This is finding #1, in one line."""
    return min(0.96, max(0.40, 0.96 - 0.058 * (lead_time_days - 1)))


# ---------------------------------------------------------------------------
# 1. Companies and people
# ---------------------------------------------------------------------------

def build_people(rng, n):
    heads = COMPANY_HEADS[:]
    tails = COMPANY_TAILS[:]
    combos = [(h, t) for h in heads for t in tails]
    rng.shuffle(combos)
    people = []
    for i in range(n):
        head, tail = combos[i]
        company = "%s %s" % (head, tail)
        slug = (head + tail).lower().replace(" ", "")
        tld = rng.choice([".com", ".com", ".com", ".io", ".co"])
        seniority = rng.choices(
            ["director", "manager", "vp", "c_suite"], weights=[31, 28, 26, 15]
        )[0]
        city, state = rng.choice(CITIES)
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        people.append({
            "first_name": first,
            "last_name": last,
            "title": rng.choice(TITLES[seniority]),
            "seniority": seniority,
            "company_name": company,
            "company_domain": slug + tld,
            "industry": rng.choice(INDUSTRIES),
            "city": city,
            "state": state,
        })
    return people


# ---------------------------------------------------------------------------
# 2. Spend, then leads allocated across spend so cost-per-lead ties out exactly
# ---------------------------------------------------------------------------

def build_spend_days(rng):
    """One row per campaign-day, with each channel's total spend pinned to
    cpl * leads so the CPL in the story is exactly the CPL in the data."""
    rows = []
    for channel, cid, name, objective, audience, start, end, share in CAMPAIGNS:
        cfg = CHANNELS[channel]
        budget = cfg["cpl"] * cfg["leads"] * share
        days = days_between(max(start, WINDOW_START), min(end, WINDOW_END))
        # Weight each day, then normalise so the campaign spends exactly `budget`.
        weights = []
        for i, day in enumerate(days):
            ramp = 0.85 + 0.30 * (i / max(1, len(days) - 1))  # scale up over the flight
            weights.append(weekday_weight(day) * ramp * rng.uniform(0.75, 1.25))
        total_w = sum(weights)
        allocated = 0.0
        for i, day in enumerate(days):
            if i == len(days) - 1:
                spend = round(budget - allocated, 2)
            else:
                spend = round(budget * weights[i] / total_w, 2)
                allocated += spend
            rows.append({
                "date": day.isoformat(),
                "platform": PLATFORM_OF[channel],
                "channel": channel,
                "campaign_id": cid,
                "campaign_name": name,
                "objective": objective,
                "audience": audience,
                "spend_usd": max(0.0, spend),
                "_day": day,
            })
    return rows


def allocate_leads_to_days(rng, spend_rows):
    """Draw each channel's leads from its campaign-days weighted by spend, so a
    day that spent more produced more leads and the join is exact."""
    by_channel = {}
    for row in spend_rows:
        by_channel.setdefault(row["channel"], []).append(row)
    assignments = {}
    for channel, cfg in CHANNELS.items():
        if channel == "organic":
            # No spend rows. Spread organic leads over business days.
            days = [d for d in days_between(WINDOW_START, WINDOW_END)]
            weights = [weekday_weight(d) for d in days]
            assignments[channel] = [
                {"day": d, "campaign_id": "", "campaign_name": "Organic and direct"}
                for d in rng.choices(days, weights=weights, k=cfg["leads"])
            ]
            continue
        rows = by_channel[channel]
        weights = [r["spend_usd"] for r in rows]
        picked = rng.choices(rows, weights=weights, k=cfg["leads"])
        assignments[channel] = [
            {"day": r["_day"], "campaign_id": r["campaign_id"],
             "campaign_name": r["campaign_name"], "_row": r}
            for r in picked
        ]
    return assignments


# ---------------------------------------------------------------------------
# 3. Leads -> the funnel
# ---------------------------------------------------------------------------

def build_leads(rng, people, assignments):
    leads = []
    idx = 0
    for channel, cfg in CHANNELS.items():
        for n, slot in enumerate(assignments[channel]):
            person = people[idx]
            idx += 1
            sources = tri_int(rng, cfg["sources"], 1, 12)
            employees = tri_int(rng, cfg["emp"], 12, 1200)
            # A BI tool is likelier the more silos you have -- and it flips the
            # pitch from "you have no dashboard" to "yours isn't connected".
            has_bi = rng.random() < min(0.82, 0.06 + 0.085 * sources)
            first = person["first_name"].lower()
            last = person["last_name"].lower()
            local = rng.choice([first, "%s.%s" % (first, last), "%s%s" % (first[0], last)])
            leads.append({
                "lead_id": "L%04d" % (len(leads) + 1),
                "created_date": slot["day"],
                "source_channel": channel,
                "campaign_id": slot["campaign_id"],
                "source_campaign": slot["campaign_name"],
                "siloed_source_count": sources,
                "bi_tool": rng.choice(BI_TOOLS) if has_bi else "",
                "employee_count": employees,
                "estimated_revenue_usd": int(round(
                    employees * rng.uniform(115000, 340000), -4)),
                "email": "%s@%s" % (local, person["company_domain"]),
                "rep": rng.choice(REPS),
                **person,
            })
    return leads


def simulate_meeting(rng, lead, meeting_type, booking_created, rep, seq):
    """One Calendly booking and what happened on it. Returns (rows, held_row)."""
    cfg = CHANNELS[lead["source_channel"]]
    spec = cfg["lead_time"] if meeting_type == "discovery" else (2, 12, 5.0)
    lead_time = tri_int(rng, spec, 0, 21)
    scheduled = booking_created + timedelta(days=lead_time)
    rows = []
    attempt = 0
    while True:
        attempt += 1
        p_show = show_probability(lead_time)
        roll = rng.random()
        if roll < p_show:
            status = "held"
        else:
            status = rng.choices(
                ["no_show", "canceled", "rescheduled"], weights=[75, 15, 10]
            )[0]
        # A rescheduled booking is only allowed to bounce once.
        if status == "rescheduled" and attempt >= 2:
            status = rng.choices(["no_show", "canceled"], weights=[60, 40])[0]
        # Showing the sample dashboard is the rep's habit, and much likelier on
        # the walkthrough than on discovery.
        shown = (status == "held"
                 and rng.random() < rep["show_sample"] * (1.0 if meeting_type == "walkthrough" else 0.80))
        rows.append({
            "meeting_id": "M%s-%d%s" % (lead["lead_id"][1:], seq, chr(96 + attempt) if attempt > 1 else ""),
            "lead_id": lead["lead_id"],
            "booking_created_at": stamp(booking_created, rng, 8, 20),
            "scheduled_at": stamp(scheduled, rng, 9, 17),
            "lead_time_days": lead_time,
            "meeting_type": meeting_type,
            "rep": rep["name"],
            "status": status,
            "duration_min": (rng.choice([25, 30, 30, 35, 40, 45, 50])
                             if status == "held" else 0),
            "sample_dashboard_shown": "true" if shown else "false",
            "sources_discussed": (max(1, lead["siloed_source_count"] + rng.choice([-1, 0, 0, 1]))
                                  if status == "held" else ""),
            "pricing_discussed": ("true" if (status == "held" and rng.random() <
                                             (0.78 if meeting_type == "walkthrough" else 0.24))
                                  else "false" if status == "held" else ""),
            "_scheduled_day": scheduled,
            "_shown": shown,
            "_status": status,
        })
        if status != "rescheduled":
            return rows, (rows[-1] if status == "held" else None)
        # Rebook: they push it out a few days from the missed slot.
        booking_created = scheduled
        lead_time = tri_int(rng, (2, 12, 5.0), 0, 21)
        scheduled = booking_created + timedelta(days=lead_time)


def run_funnel(rng, leads):
    """Walk every lead through booking -> show -> close, and record the deal."""
    meetings = []
    for lead in leads:
        cfg = CHANNELS[lead["source_channel"]]
        rep = lead["rep"]
        lead["deal_stage"] = "new"
        lead["lifecycle_stage"] = "lead"
        lead["demo_booked"] = "false"
        lead["closed_won"] = "false"
        lead["closed_date"] = ""
        lead["tier"] = ""
        lead["deal_amount_mrr"] = ""
        lead["contract_length_months"] = ""
        lead["lost_reason"] = ""
        lead["stripe_customer_id"] = ""

        if rng.random() >= cfg["book"]:
            lead["deal_stage"] = rng.choices(
                ["contacted", "new"], weights=[70, 30])[0]
            lead["lifecycle_stage"] = "mql" if lead["deal_stage"] == "contacted" else "lead"
            lead["lost_reason"] = "" if lead["deal_stage"] == "new" else "Never booked a call"
            continue

        lead["demo_booked"] = "true"
        lead["deal_stage"] = "demo_scheduled"
        lead["lifecycle_stage"] = "sql"
        booking_created = lead["created_date"] + timedelta(days=rng.randint(0, 5))
        rows1, held1 = simulate_meeting(rng, lead, "discovery", booking_created, rep, 1)
        meetings.extend(rows1)
        held = [held1] if held1 else []

        if held1:
            lead["deal_stage"] = "demo_held"
            if rng.random() < cfg["advance"]:
                lead["deal_stage"] = "proposal"
                lead["lifecycle_stage"] = "opportunity"
                nxt = held1["_scheduled_day"] + timedelta(days=rng.randint(1, 6))
                rows2, held2 = simulate_meeting(rng, lead, "walkthrough", nxt, rep, 2)
                meetings.extend(rows2)
                if held2:
                    held.append(held2)

        if not held:
            lead["deal_stage"] = "closed_lost"
            lead["lifecycle_stage"] = "disqualified"
            lead["lost_reason"] = "No show, unresponsive"
            _finish_meetings(rows1, "closed_lost")
            continue

        # Finding #2: the sample dashboard roughly doubles the close rate.
        shown_any = any(m["_shown"] for m in held)
        source_factor = min(1.18, 0.80 + 0.032 * lead["siloed_source_count"])
        p_close = cfg["close"] * (1.75 if shown_any else 0.80) * source_factor
        p_close = min(0.92, max(0.02, p_close))
        won = rng.random() < p_close

        last = held[-1]
        for m in held[:-1]:
            m["outcome"] = "advanced"
            m["next_step"] = "Walkthrough booked"
        if won:
            lead["closed_won"] = "true"
            lead["deal_stage"] = "closed_won"
            lead["lifecycle_stage"] = "customer"
            closed = last["_scheduled_day"] + timedelta(days=rng.randint(1, 14))
            if closed > WINDOW_END:
                closed = WINDOW_END
            lead["closed_date"] = closed
            tier = tier_for_sources(lead["siloed_source_count"], rng)
            lead["tier"] = tier
            lead["deal_amount_mrr"] = TIERS[tier]["mrr"]
            lead["contract_length_months"] = rng.choices([12, 6], weights=[78, 22])[0]
            last["outcome"] = "closed_won"
            last["next_step"] = "Kickoff and stack audit scheduled"
        else:
            lead["deal_stage"] = "closed_lost"
            lead["lifecycle_stage"] = "disqualified"
            reason = ("Not enough data sources to justify it"
                      if lead["siloed_source_count"] <= 3 and rng.random() < 0.55
                      else rng.choice(LOST_REASONS))
            lead["lost_reason"] = reason
            last["outcome"] = rng.choices(
                ["closed_lost", "nurture"], weights=[72, 28])[0]
            last["next_step"] = ("Nurture, revisit next quarter"
                                 if last["outcome"] == "nurture" else "Closed lost")
            last["objection"] = ""
        _finish_meetings(meetings, None)
    return meetings


def _finish_meetings(rows, forced_outcome):
    """Fill outcome/next_step/objection on any row that didn't get one."""
    for m in rows:
        m.setdefault("objection", "")
        if "outcome" in m and m["outcome"]:
            continue
        if m["_status"] == "held":
            m["outcome"] = forced_outcome or "advanced"
            m["next_step"] = m.get("next_step") or "Follow up"
        elif m["_status"] == "no_show":
            m["outcome"] = "no_show"
            m["next_step"] = "Re-invite sent"
        elif m["_status"] == "canceled":
            m["outcome"] = "canceled"
            m["next_step"] = "Asked to rebook"
        else:
            m["outcome"] = "rescheduled"
            m["next_step"] = "Rebooked"


# ---------------------------------------------------------------------------
# 4. Web sessions -- the traffic side of the same leads
# ---------------------------------------------------------------------------

SESSION_SOURCE = {
    "meta_ads": ("Paid Social", "facebook", "paid_social"),
    "google_ads": ("Paid Search", "google", "cpc"),
    "linkedin_ads": ("Paid Social", "linkedin", "paid_social"),
    "cold_email": ("Email", "instantly", "email"),
    "organic": ("Organic Search", "google", "organic"),
}

ANON_MIX = [
    ("Organic Search", "google", "organic"),
    ("Direct", "(direct)", "(none)"),
    ("Referral", "news.ycombinator.com", "referral"),
    ("Referral", "linkedin.com", "referral"),
    ("Organic Social", "linkedin", "social"),
]


def _session(rng, sid, vid, day, group, source, medium, campaign_id, campaign_name,
             converted, lead=None, rb2b=False):
    if converted or rb2b:
        pages = rng.randint(3, 11)
        duration = rng.randint(95, 620)
        landing = rng.choice(LANDING_PAGES)
        scrolled = rng.random() < 0.72
    else:
        pages = rng.choices([1, 2, 3, 4], weights=[52, 26, 14, 8])[0]
        duration = rng.randint(6, 190)
        landing = rng.choice(LANDING_PAGES)
        scrolled = rng.random() < 0.18
    return {
        "session_id": sid,
        "visitor_id": vid,
        "session_start": stamp(day, rng, 7, 21),
        "channel_group": group,
        "utm_source": source,
        "utm_medium": medium,
        "utm_campaign": campaign_name,
        "campaign_id": campaign_id,
        "device": rng.choices(["desktop", "mobile", "tablet"], weights=[68, 28, 4])[0],
        "country": "US",
        "region": rng.choice(CITIES)[1] if lead is None else lead["state"],
        "landing_page": landing,
        "pages_viewed": pages,
        "session_duration_sec": duration,
        "scrolled_pricing": "true" if scrolled else "false",
        "form_submitted": "true" if converted else "false",
        "identified_by_rb2b": "true" if rb2b else "false",
        "company_domain": lead["company_domain"] if lead else "",
        "lead_id": lead["lead_id"] if lead else "",
    }


def build_sessions(rng, leads, filler_count=260):
    sessions = []
    counter = [0, 0]

    def ids():
        counter[0] += 1
        counter[1] += 1
        return "S%05d" % counter[0], "V%05d" % counter[1]

    for lead in leads:
        channel = lead["source_channel"]
        day = lead["created_date"]
        cid = lead["campaign_id"]
        cname = lead["source_campaign"] if cid else "(not set)"

        if channel == "rb2b_visitor_id":
            # No form fill -- RB2B resolved them from the anonymous session.
            group, source, medium = rng.choice(ANON_MIX)
            sid, vid = ids()
            sessions.append(_session(rng, sid, vid, day, group, source, medium,
                                     cid, cname, False, lead, rb2b=True))
            lead["session_id"] = sid
            if rng.random() < 0.34:
                earlier = day - timedelta(days=rng.randint(1, 21))
                if earlier >= WINDOW_START:
                    sid2, _ = ids()
                    sessions.append(_session(rng, sid2, vid, earlier, group, source,
                                             medium, cid, cname, False, lead, rb2b=False))
            continue

        if channel == "cold_email":
            # Cold leads only appear in analytics if they clicked the email.
            if rng.random() < 0.55:
                group, source, medium = SESSION_SOURCE[channel]
                sid, vid = ids()
                converted = lead["demo_booked"] == "true"
                sessions.append(_session(rng, sid, vid, day, group, source, medium,
                                         cid, cname, converted, lead))
                lead["session_id"] = sid
            else:
                lead["session_id"] = ""
            continue

        group, source, medium = SESSION_SOURCE[channel]
        sid, vid = ids()
        sessions.append(_session(rng, sid, vid, day, group, source, medium,
                                 cid, cname, True, lead))
        lead["session_id"] = sid
        # A research visit before the one that converted, same visitor_id, so
        # first-click vs last-click is answerable without dirtying any join.
        if rng.random() < 0.28:
            earlier = day - timedelta(days=rng.randint(1, 24))
            if earlier >= WINDOW_START:
                sid2, _ = ids()
                g2, s2, m2 = rng.choice(ANON_MIX)
                sessions.append(_session(rng, sid2, vid, earlier, g2, s2, m2,
                                         "", "(not set)", False, None))

    days = days_between(WINDOW_START, WINDOW_END)
    weights = [weekday_weight(d) for d in days]
    for day in rng.choices(days, weights=weights, k=filler_count):
        group, source, medium = rng.choices(
            ANON_MIX + [SESSION_SOURCE["meta_ads"], SESSION_SOURCE["google_ads"]],
            weights=[22, 20, 6, 9, 8, 20, 15])[0]
        sid, vid = ids()
        sessions.append(_session(rng, sid, vid, day, group, source, medium,
                                 "", "(not set)", False, None))

    sessions.sort(key=lambda s: s["session_start"])
    return sessions


# ---------------------------------------------------------------------------
# 5. Stripe -- one row per subscription per billing month
# ---------------------------------------------------------------------------

def add_months(day, n):
    month = day.month - 1 + n
    year = day.year + month // 12
    month = month % 12 + 1
    last = [31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last))


def build_subscriptions(rng, leads):
    rows = []
    won = [l for l in leads if l["closed_won"] == "true"]
    won.sort(key=lambda l: l["closed_date"])
    for n, lead in enumerate(won, start=1):
        cust = "cus_%s%04d" % (lead["company_domain"][:3].upper(), n)
        sub = "sub_%s%04d" % (lead["company_domain"][:3].lower(), n)
        lead["stripe_customer_id"] = cust

        tier = lead["tier"]
        mrr = TIERS[tier]["mrr"]
        connected = max(2, min(lead["siloed_source_count"], TIERS[tier]["max_sources"]))
        start = min(lead["closed_date"] + timedelta(days=rng.randint(3, 12)), WINDOW_END)

        canceled_at = ""
        churn_reason = ""
        pending = []
        month = 0
        past_due = False
        lifetime = subscription_lifetime(rng, connected)
        while True:
            invoice_date = add_months(start, month)
            if invoice_date > WINDOW_END:
                break
            status = "paid"
            if past_due:
                # Dunning: most recover, the rest churn on the failed invoice.
                if rng.random() < 0.70:
                    status = "paid"
                    past_due = False
                else:
                    canceled_at = invoice_date.isoformat()
                    churn_reason = "Budget cut"
                    status = "failed"
            elif rng.random() < 0.04:
                status = "failed"
                past_due = True
            pending.append({
                "invoice_id": "in_%s%04d%02d" % (lead["company_domain"][:3].lower(), n, month),
                "stripe_customer_id": cust,
                "subscription_id": sub,
                "lead_id": lead["lead_id"],
                "company_domain": lead["company_domain"],
                "email": lead["email"],
                "billing_month": invoice_date.strftime("%Y-%m"),
                "invoice_date": invoice_date.isoformat(),
                "plan_tier": tier,
                "mrr_usd": mrr,
                "connected_sources": connected,
                "subscription_started_at": start.isoformat(),
                "invoice_status": status,
                "months_since_start": month,
                "is_first_invoice": "true" if month == 0 else "false",
            })
            if canceled_at:
                break
            if month + 1 >= lifetime:
                canceled_at = add_months(start, month + 1).isoformat()
                churn_reason = ("Not enough sources to justify cost"
                                if connected <= 4 and rng.random() < 0.6
                                else rng.choice(CHURN_REASONS))
                break
            # Expansion: connecting another source can push them up a tier, and
            # buys back some runway -- more unified silos, more reason to stay.
            if rng.random() < 0.055 and connected < 12:
                connected += 1
                lifetime += rng.randint(1, 3)
                if connected > TIERS[tier]["max_sources"]:
                    tier = "Growth" if tier == "Starter" else "Enterprise"
                    mrr = TIERS[tier]["mrr"]
            month += 1

        sub_status = "canceled" if canceled_at else ("past_due" if past_due else "active")
        for row in pending:
            row["subscription_status"] = sub_status
            row["canceled_at"] = canceled_at
            row["churn_reason"] = churn_reason
            rows.append(row)
    rows.sort(key=lambda r: (r["invoice_date"], r["stripe_customer_id"]))
    return rows


# ---------------------------------------------------------------------------
# 6. Finish the spend rows and write everything out
# ---------------------------------------------------------------------------

# Per-platform delivery economics, used to back impressions and clicks out of
# spend. Instantly and RB2B have no auction, so those columns stay blank.
DELIVERY = {
    "Meta Ads": {"cpc": 2.40, "ctr": 0.0135},
    "Google Ads": {"cpc": 9.50, "ctr": 0.0420},
    "LinkedIn Ads": {"cpc": 14.50, "ctr": 0.0062},
}


def week_start(day):
    return day - timedelta(days=day.weekday())


def finish_spend_rows(rng, spend_rows, leads):
    """Roll the daily spend simulation up to campaign-weeks, and count the leads
    each campaign-week produced so cost_per_lead ties out against crm_deals."""
    counts = {}
    for lead in leads:
        if lead["campaign_id"]:
            key = (lead["campaign_id"], week_start(lead["created_date"]))
            counts[key] = counts.get(key, 0) + 1

    weeks = {}
    for row in spend_rows:
        key = (row["campaign_id"], week_start(row["_day"]))
        bucket = weeks.get(key)
        if bucket is None:
            bucket = weeks[key] = dict(row)
            bucket["spend_usd"] = 0.0
            bucket["active_days"] = 0
        bucket["spend_usd"] += row["spend_usd"]
        bucket["active_days"] += 1

    out = []
    for (cid, wk), row in weeks.items():
        spend = round(row["spend_usd"], 2)
        n_leads = counts.get((cid, wk), 0)
        row["week_start"] = wk.isoformat()
        delivery = DELIVERY.get(row["platform"])
        if delivery:
            cpc = delivery["cpc"] * rng.uniform(0.82, 1.22)
            ctr = delivery["ctr"] * rng.uniform(0.80, 1.25)
            clicks = max(0, int(round(spend / cpc)))
            impressions = int(round(clicks / ctr)) if clicks else int(round(spend * 60))
            lpv = int(round(clicks * rng.uniform(0.82, 0.95)))
            out.append({
                "week_start": row["week_start"],
                "active_days": row["active_days"],
                "platform": row["platform"],
                "channel": row["channel"],
                "campaign_id": row["campaign_id"],
                "campaign_name": row["campaign_name"],
                "objective": row["objective"],
                "audience": row["audience"],
                "spend_usd": "%.2f" % spend,
                "impressions": impressions,
                "clicks": clicks,
                "ctr": "%.4f" % (clicks / impressions) if impressions else "",
                "cpc": "%.2f" % (spend / clicks) if clicks else "",
                "landing_page_views": lpv,
                "leads": n_leads,
                "cost_per_lead": "%.2f" % (spend / n_leads) if n_leads else "",
                "utm_campaign": row["campaign_name"],
            })
        else:
            out.append({
                "week_start": row["week_start"],
                "active_days": row["active_days"],
                "platform": row["platform"],
                "channel": row["channel"],
                "campaign_id": row["campaign_id"],
                "campaign_name": row["campaign_name"],
                "objective": row["objective"],
                "audience": row["audience"],
                "spend_usd": "%.2f" % spend,
                "impressions": "",
                "clicks": "",
                "ctr": "",
                "cpc": "",
                "landing_page_views": "",
                "leads": n_leads,
                "cost_per_lead": "%.2f" % (spend / n_leads) if n_leads else "",
                "utm_campaign": row["campaign_name"],
            })
    out.sort(key=lambda r: (r["week_start"], r["campaign_id"]))
    return out


ADS_COLUMNS = [
    "week_start", "platform", "channel", "campaign_id", "campaign_name",
    "objective", "audience", "active_days", "spend_usd", "impressions",
    "clicks", "ctr", "cpc", "landing_page_views", "leads", "cost_per_lead",
    "utm_campaign",
]

SESSION_COLUMNS = [
    "session_id", "visitor_id", "session_start", "channel_group", "utm_source",
    "utm_medium", "utm_campaign", "campaign_id", "device", "country", "region",
    "landing_page", "pages_viewed", "session_duration_sec", "scrolled_pricing",
    "form_submitted", "identified_by_rb2b", "company_domain", "lead_id",
]

CRM_COLUMNS = [
    "lead_id", "created_date", "first_name", "last_name", "email", "title",
    "seniority", "company_name", "company_domain", "industry", "employee_count",
    "estimated_revenue_usd", "city", "state", "source_channel",
    "source_campaign", "campaign_id", "session_id", "siloed_source_count",
    "bi_tool", "lifecycle_stage", "deal_stage", "deal_owner", "demo_booked",
    "closed_date", "closed_won", "tier", "deal_amount_mrr",
    "contract_length_months", "lost_reason", "stripe_customer_id",
]

CALL_COLUMNS = [
    "meeting_id", "lead_id", "booking_created_at", "scheduled_at",
    "lead_time_days", "meeting_type", "rep", "status", "duration_min",
    "sample_dashboard_shown", "sources_discussed", "pricing_discussed",
    "objection", "outcome", "next_step",
]

STRIPE_COLUMNS = [
    "invoice_id", "stripe_customer_id", "subscription_id", "lead_id",
    "company_domain", "email", "billing_month", "invoice_date", "plan_tier",
    "mrr_usd", "connected_sources", "subscription_started_at",
    "subscription_status", "invoice_status", "months_since_start",
    "is_first_invoice", "canceled_at", "churn_reason",
]


def write_csv(path, columns, rows):
    # utf-8-sig so Excel opens these cleanly, matching export_leads_csv.py.
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


def crm_rows(leads):
    out = []
    for lead in leads:
        row = dict(lead)
        row["created_date"] = lead["created_date"].isoformat()
        row["closed_date"] = (lead["closed_date"].isoformat()
                              if lead["closed_date"] else "")
        row["deal_owner"] = lead["rep"]["name"] if lead["demo_booked"] == "true" else ""
        row["session_id"] = lead.get("session_id", "")
        out.append(row)
    out.sort(key=lambda r: r["lead_id"])
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / ".tmp" / "mock_platform_data"),
                        help="output directory (default: .tmp/mock_platform_data)")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--scale", type=float, default=1.0,
                        help="scale every channel's lead volume (default 1.0). "
                             "Below ~0.6 the per-channel retention cohorts get "
                             "too thin to read.")
    args = parser.parse_args()

    if args.scale != 1.0:
        for cfg in CHANNELS.values():
            cfg["leads"] = max(1, int(round(cfg["leads"] * args.scale)))

    rng = Random(args.seed)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    total_leads = sum(c["leads"] for c in CHANNELS.values())
    people = build_people(rng, total_leads)
    spend_rows = build_spend_days(rng)
    assignments = allocate_leads_to_days(rng, spend_rows)
    leads = build_leads(rng, people, assignments)
    meetings = run_funnel(rng, leads)
    sessions = build_sessions(rng, leads)
    subscriptions = build_subscriptions(rng, leads)
    ads = finish_spend_rows(rng, spend_rows, leads)

    # Objections belong on the calls that lost, so coaching has something to read.
    for m in meetings:
        if m["outcome"] in ("closed_lost", "nurture") and not m["objection"]:
            m["objection"] = rng.choice(OBJECTIONS)
    meetings.sort(key=lambda m: (m["scheduled_at"], m["meeting_id"]))

    counts = {
        "ADS_CSV": (out_dir / "ad_platform_spend.csv", ADS_COLUMNS, ads),
        "SESSIONS_CSV": (out_dir / "web_sessions.csv", SESSION_COLUMNS, sessions),
        "CRM_CSV": (out_dir / "crm_deals.csv", CRM_COLUMNS, crm_rows(leads)),
        "CALLS_CSV": (out_dir / "sales_calls.csv", CALL_COLUMNS, meetings),
        "STRIPE_CSV": (out_dir / "stripe_subscriptions.csv", STRIPE_COLUMNS, subscriptions),
    }
    total = 0
    for key, (path, columns, rows) in counts.items():
        n = write_csv(path, columns, rows)
        total += n
        print("%s=%s" % (key, path))
        print("%s_ROWS=%d" % (key[:-4], n))
    print("OUT_DIR=%s" % out_dir)
    print("ROWS_TOTAL=%d" % total)


if __name__ == "__main__":
    main()
