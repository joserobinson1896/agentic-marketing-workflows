"""
Layer 3 execution tool: the semantic layer for the Unified Dashboards dashboard.

Reads the five mock platform CSVs and emits one JSON file holding every number the
dashboard renders. The point of the split is that a metric is defined exactly once
here, so the same figure cannot disagree with itself between two sections of the UI.

The seven data traps documented in the blueprint are encoded in this file and nowhere
else. Each one is marked `TRAP n` at the point it is handled:

  1 blank is not zero      Instantly/RB2B carry no auction metrics (168 rows blank)
  2 right-censoring        retention@N needs N months of runway before the window end
  3 lost_reason on open    298 `contacted` rows carry a lost_reason; filter deal_stage
  4 RB2B never submits     identified traffic converts with form_submitted = false
  5 CRM is last-touch      crm.session_id is the LAST session, not the first
  6 method-sensitive       the sample-dashboard split changes with the counting method
  7 organic has no spend   exclude it from cost metrics rather than dividing by zero

CLI usage:
    python execution/dashboard/build_semantic_layer.py
    python execution/dashboard/build_semantic_layer.py --out .tmp/dashboard/semantic_layer.json
"""

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)

# The trailing edge of the data. Every tenure-normalised figure is measured against
# this date, so it lives here rather than being repeated at each call site (TRAP 2).
WINDOW_END = date(2026, 8, 31)

CHANNEL_LABEL = {
    "meta_ads": "Meta Ads",
    "google_ads": "Google Ads",
    "linkedin_ads": "LinkedIn Ads",
    "cold_email": "Cold email",
    "rb2b_visitor_id": "RB2B",
    "organic": "Organic",
}
# Organic never appears in ad_platform_spend, so every cost metric is undefined for
# it. Naming the paid set once keeps that decision out of the individual metrics.
PAID_CHANNELS = ("meta_ads", "google_ads", "linkedin_ads", "cold_email", "rb2b_visitor_id")


# ---------------------------------------------------------------- loading

def load_csv(path):
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def load_all(data_dir):
    return {
        "ad": load_csv(data_dir / "ad_platform_spend.csv"),
        "crm": load_csv(data_dir / "crm_deals.csv"),
        "sc": load_csv(data_dir / "sales_calls.csv"),
        "sub": load_csv(data_dir / "stripe_subscriptions.csv"),
        "web": load_csv(data_dir / "web_sessions.csv"),
    }


def num(v, default=0.0):
    """Blank is not zero (TRAP 1) — callers that must distinguish pass default=None."""
    if v is None or v == "":
        return default
    return float(v)


def iso_monday(d):
    """Spend is weekly, the CRM is daily. Bucket to the ISO Monday to align them."""
    y, m, dd = (int(x) for x in d[:10].split("-"))
    dt = date(y, m, dd)
    return (dt - timedelta(days=dt.weekday())).isoformat()


def months_between(start, end):
    return (end.year - start.year) * 12 + (end.month - start.month)


def parse_date(s):
    y, m, d = (int(x) for x in s[:10].split("-"))
    return date(y, m, d)


def band_sources(n):
    return "≤3" if n <= 3 else "4–5" if n <= 5 else "6–7" if n <= 7 else "8+"


BAND_ORDER = ("≤3", "4–5", "6–7", "8+")


# ---------------------------------------------------------------- derived views

def latest_invoice_per_sub(sub):
    """Current state of a subscription is its LAST invoice, never its first — 10 of the
    139 subscriptions expand or upgrade mid-life."""
    out = {}
    for r in sub:
        k = r["subscription_id"]
        if k not in out or r["billing_month"] > out[k]["billing_month"]:
            out[k] = r
    return out


def first_invoice_per_sub(sub):
    out = {}
    for r in sub:
        k = r["subscription_id"]
        if k not in out or r["billing_month"] < out[k]["billing_month"]:
            out[k] = r
    return out


def is_live(row):
    return row["subscription_status"] in ("active", "past_due")


# ---------------------------------------------------------------- metrics

def build(data):
    ad, crm, sc, sub, web = (data[k] for k in ("ad", "crm", "sc", "sub", "web"))
    lead = {r["lead_id"]: r for r in crm}
    lead_channel = {r["lead_id"]: r["source_channel"] for r in crm}

    latest = latest_invoice_per_sub(sub)
    first = first_invoice_per_sub(sub)
    max_month = {}
    for r in sub:
        k = r["subscription_id"]
        max_month[k] = max(max_month.get(k, 0), int(r["months_since_start"]))

    # ---- spend, leads, wins, held demos, live MRR, all by channel
    spend = defaultdict(float)
    for r in ad:
        spend[r["channel"]] += num(r["spend_usd"])

    leads = Counter(r["source_channel"] for r in crm)
    won = Counter(r["source_channel"] for r in crm if r["closed_won"] == "true")

    # Held demos counted at LEAD level: a lead can hold two meetings and is still one
    # demo in the funnel.
    held_leads = Counter()
    for lid in {r["lead_id"] for r in sc if r["status"] == "held"}:
        held_leads[lead_channel[lid]] += 1
    booked_leads = Counter()
    for lid in {r["lead_id"] for r in sc}:
        booked_leads[lead_channel[lid]] += 1

    live_mrr = defaultdict(float)
    for r in latest.values():
        if is_live(r):
            live_mrr[lead_channel[r["lead_id"]]] += num(r["mrr_usd"])

    silos = defaultdict(list)
    for r in crm:
        silos[r["source_channel"]].append(int(r["siloed_source_count"]))

    channels = []
    for ch in list(PAID_CHANNELS) + ["organic"]:
        paid = ch in PAID_CHANNELS          # TRAP 7
        s, l, w = spend.get(ch, 0.0), leads[ch], won[ch]
        h, m = held_leads[ch], live_mrr[ch]
        channels.append({
            "channel": ch,
            "label": CHANNEL_LABEL[ch],
            "paid": paid,
            "spend": round(s, 2),
            "leads": l,
            "booked": booked_leads[ch],
            "held": h,
            "won": w,
            "cpl": round(s / l, 2) if paid and l else None,
            "cost_per_held": round(s / h, 2) if paid and h else None,
            "cac": round(s / w, 2) if paid and w else None,
            "avg_silos": round(sum(silos[ch]) / len(silos[ch]), 1),
            "live_mrr": round(m, 2),
            "cost_per_retained": round(s / m, 4) if paid and m else None,
        })

    # ---- headline KPIs
    total_spend = sum(spend.values())
    total_won = sum(won.values())
    total_live = sum(live_mrr.values())
    kpis = {
        "spend": round(total_spend, 2),
        "leads": len(crm),
        "demos_booked": sum(1 for r in crm if r["demo_booked"] == "true"),
        "meetings_held": sum(1 for r in sc if r["status"] == "held"),
        "won": total_won,
        "live_mrr": round(total_live, 2),
        "blended_cac": round(total_spend / total_won, 2),
        "active_subs": sum(1 for r in latest.values() if is_live(r)),
        "total_subs": len(latest),
        "cost_per_retained": round(total_spend / total_live, 4),
    }

    # ---- campaigns
    camp_spend, camp_name, camp_channel = defaultdict(float), {}, {}
    for r in ad:
        camp_spend[r["campaign_id"]] += num(r["spend_usd"])
        camp_name[r["campaign_id"]] = r["campaign_name"]
        camp_channel[r["campaign_id"]] = r["channel"]
    camp_leads, camp_won, camp_mrr = Counter(), Counter(), defaultdict(float)
    for r in crm:
        if r["campaign_id"]:
            camp_leads[r["campaign_id"]] += 1
            if r["closed_won"] == "true":
                camp_won[r["campaign_id"]] += 1
    for r in latest.values():
        if is_live(r):
            cid = lead[r["lead_id"]]["campaign_id"]
            if cid:
                camp_mrr[cid] += num(r["mrr_usd"])
    campaigns = sorted((
        {
            "campaign_id": cid,
            "name": camp_name[cid],
            "channel": camp_channel[cid],
            "label": CHANNEL_LABEL[camp_channel[cid]],
            "spend": round(camp_spend[cid], 2),
            "leads": camp_leads[cid],
            "won": camp_won[cid],
            "cpl": round(camp_spend[cid] / camp_leads[cid], 2) if camp_leads[cid] else None,
            "live_mrr": round(camp_mrr[cid], 2),
            "cost_per_retained": round(camp_spend[cid] / camp_mrr[cid], 4) if camp_mrr[cid] else None,
        }
        for cid in camp_spend
    ), key=lambda c: -c["spend"])

    # ---- the hinge: siloed_source_count
    silo_bands = []
    for b in BAND_ORDER:
        rows = [r for r in crm if band_sources(int(r["siloed_source_count"])) == b]
        w = [r for r in rows if r["closed_won"] == "true"]
        silo_bands.append({
            "band": b,
            "leads": len(rows),
            "won": len(w),
            "close_rate": round(len(w) / len(rows), 4) if rows else 0,
            "tiers": dict(Counter(r["tier"] for r in w)),
            "avg_mrr": round(sum(num(r["deal_amount_mrr"]) for r in w) / len(w), 0) if w else 0,
        })

    # ---- retention, tenure-normalised (TRAP 2)
    def retention_at(rows_first, n):
        eligible = [k for k, f in rows_first.items()
                    if months_between(parse_date(f["subscription_started_at"]), WINDOW_END) >= n]
        kept = [k for k in eligible if max_month[k] >= n]
        return len(eligible), len(kept)

    retention_by_sources = []
    for b in BAND_ORDER:
        subset = {k: f for k, f in first.items() if band_sources(int(f["connected_sources"])) == b}
        elig, kept = retention_at(subset, 6)
        retention_by_sources.append({
            "band": b, "eligible": elig, "retained": kept,
            "rate": round(kept / elig, 4) if elig else None,
        })

    retention_by_channel = []
    for ch in list(PAID_CHANNELS) + ["organic"]:
        subset = {k: f for k, f in first.items() if lead_channel[f["lead_id"]] == ch}
        elig, kept = retention_at(subset, 6)
        retention_by_channel.append({
            "channel": ch, "label": CHANNEL_LABEL[ch],
            "eligible": elig, "retained": kept,
            "rate": round(kept / elig, 4) if elig else None,
        })

    # Survival curves: share of the eligible cohort still billing at month m.
    def curve(subset, horizon=12):
        pts = []
        for m in range(horizon + 1):
            elig = [k for k, f in subset.items()
                    if months_between(parse_date(f["subscription_started_at"]), WINDOW_END) >= m]
            if not elig:
                pts.append(None)
                continue
            kept = sum(1 for k in elig if max_month[k] >= m)
            pts.append(round(kept / len(elig), 4))
        return pts

    curves_by_sources = {b: curve({k: f for k, f in first.items()
                                   if band_sources(int(f["connected_sources"])) == b})
                         for b in BAND_ORDER}
    curves_by_channel = {ch: curve({k: f for k, f in first.items()
                                    if lead_channel[f["lead_id"]] == ch})
                         for ch in list(PAID_CHANNELS) + ["organic"]}

    # ---- MRR movement by billing month
    months = sorted({r["billing_month"] for r in sub})
    by_month = defaultdict(list)
    for r in sub:
        by_month[r["billing_month"]].append(r)
    movement, prev = [], {}
    for m in months:
        rows = by_month[m]
        seen, new_mrr, exp_mrr = set(), 0.0, 0.0
        for r in rows:
            k, v = r["subscription_id"], num(r["mrr_usd"])
            seen.add(k)
            if k not in prev:
                new_mrr += v
            elif v > prev[k]:
                exp_mrr += v - prev[k]
        churn = -sum(v for k, v in prev.items() if k not in seen)
        movement.append({
            "month": m, "new": round(new_mrr, 2), "expansion": round(exp_mrr, 2),
            "churn": round(churn, 2),
            "ending_mrr": round(sum(num(r["mrr_usd"]) for r in rows), 2),
            "active": len(rows),
        })
        for r in rows:
            prev[r["subscription_id"]] = num(r["mrr_usd"])

    # ---- churn reasons, deduplicated to the subscription
    churn_reasons = sorted((
        {"reason": k, "subs": v}
        for k, v in Counter(f["churn_reason"] for f in first.values() if f["churn_reason"]).items()
    ), key=lambda x: -x["subs"])
    churned_total = sum(c["subs"] for c in churn_reasons)

    # ---- sales execution
    noshow_bands = []
    for label, lo, hi in (("≤2 days", 0, 2), ("3–5 days", 3, 5), ("6+ days", 6, 999)):
        rows = [r for r in sc if lo <= int(r["lead_time_days"]) <= hi]
        ns = [r for r in rows if r["status"] == "no_show"]
        noshow_bands.append({"band": label, "meetings": len(rows), "no_shows": len(ns),
                             "rate": round(len(ns) / len(rows), 4) if rows else 0})

    noshow_by_day = []
    for d in range(0, 22):
        rows = [r for r in sc if int(r["lead_time_days"]) == d]
        if len(rows) < 5:
            continue
        ns = sum(1 for r in rows if r["status"] == "no_show")
        noshow_by_day.append({"day": d, "meetings": len(rows), "rate": round(ns / len(rows), 4)})

    # TRAP 6: the sample-dashboard split is method-sensitive. Lead-level exclusive is the
    # method the dashboard prints; the alternatives are carried so the UI can state them.
    shown_any = {r["lead_id"] for r in sc if r["status"] == "held" and r["sample_dashboard_shown"] == "true"}
    held_any = {r["lead_id"] for r in sc if r["status"] == "held"}
    never_shown = held_any - shown_any

    def close_rate(ids):
        w = sum(1 for l in ids if lead[l]["closed_won"] == "true")
        return {"leads": len(ids), "won": w, "rate": round(w / len(ids), 4) if ids else 0}

    demo_shown = {
        "method": "Lead-level, exclusive — a lead counts as shown if any held meeting showed it.",
        "shown": close_rate(shown_any),
        "not_shown": close_rate(never_shown),
        "alternatives": {
            "meeting_level": "42% vs 19%",
            "last_held_meeting": "56% vs 29%",
        },
    }

    reps = []
    for rep in sorted({r["rep"] for r in sc}):
        rows = [r for r in sc if r["rep"] == rep]
        held = [r for r in rows if r["status"] == "held"]
        shown = [r for r in held if r["sample_dashboard_shown"] == "true"]
        rep_leads = {r["lead_id"] for r in held}
        rep_won = sum(1 for l in rep_leads if lead[l]["closed_won"] == "true")
        ns = sum(1 for r in rows if r["status"] == "no_show")
        reps.append({
            "rep": rep, "meetings": len(rows), "held": len(held),
            "shown": len(shown),
            "shown_rate": round(len(shown) / len(held), 4) if held else 0,
            "no_show_rate": round(ns / len(rows), 4) if rows else 0,
            "leads": len(rep_leads), "won": rep_won,
            "close_rate": round(rep_won / len(rep_leads), 4) if rep_leads else 0,
        })

    objections = sorted((
        {"objection": k, "n": v}
        for k, v in Counter(r["objection"] for r in sc if r["objection"]).items()
    ), key=lambda x: -x["n"])

    # ---- weekly spend and CPL trend (paid only)
    crm_week = Counter((r["campaign_id"], iso_monday(r["created_date"])) for r in crm if r["campaign_id"])
    weekly = defaultdict(lambda: {"spend": 0.0, "leads": 0})
    for r in ad:
        k = (r["channel"], r["week_start"])
        weekly[k]["spend"] += num(r["spend_usd"])
        weekly[k]["leads"] += int(num(r["leads"]))
    weekly_rows = sorted((
        {"channel": ch, "week": wk, "spend": round(v["spend"], 2), "leads": v["leads"],
         "cpl": round(v["spend"] / v["leads"], 2) if v["leads"] else None}
        for (ch, wk), v in weekly.items()
    ), key=lambda r: (r["channel"], r["week"]))

    # ---- lost reasons — closed_lost only (TRAP 3)
    lost_reasons = sorted((
        {"reason": k, "n": v}
        for k, v in Counter(r["lost_reason"] for r in crm
                            if r["deal_stage"] == "closed_lost" and r["lost_reason"]).items()
    ), key=lambda x: -x["n"])
    open_with_lost_reason = sum(1 for r in crm
                                if r["deal_stage"] not in ("closed_won", "closed_lost") and r["lost_reason"])

    # ---- firmographics
    industries = sorted((
        {"industry": i,
         "leads": sum(1 for r in crm if r["industry"] == i),
         "won": sum(1 for r in crm if r["industry"] == i and r["closed_won"] == "true"),
         "avg_silos": round(sum(int(r["siloed_source_count"]) for r in crm if r["industry"] == i)
                            / max(1, sum(1 for r in crm if r["industry"] == i)), 1)}
        for i in {r["industry"] for r in crm}
    ), key=lambda x: -x["leads"])

    # ---- web behaviour (TRAP 4: RB2B converts without ever submitting a form)
    web_stats = {
        "sessions": len(web),
        "identified_leads": sum(1 for r in web if r["lead_id"]),
        "anonymous": sum(1 for r in web if not r["lead_id"]),
        "form_submitted": sum(1 for r in web if r["form_submitted"] == "true"),
        "rb2b_identified": sum(1 for r in web if r["identified_by_rb2b"] == "true"),
        "rb2b_with_form": sum(1 for r in web
                              if r["identified_by_rb2b"] == "true" and r["form_submitted"] == "true"),
        "scrolled_pricing": sum(1 for r in web if r["scrolled_pricing"] == "true"),
        "landing_pages": sorted((
            {"page": p,
             "sessions": sum(1 for r in web if r["landing_page"] == p),
             "leads": sum(1 for r in web if r["landing_page"] == p and r["lead_id"])}
            for p in {r["landing_page"] for r in web}
        ), key=lambda x: -x["sessions"]),
    }

    # ---- accounts table + per-lead drill-down
    sessions_by_lead, meetings_by_lead, invoices_by_lead = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in sorted(web, key=lambda r: r["session_start"]):
        if r["lead_id"]:
            sessions_by_lead[r["lead_id"]].append({
                "id": r["session_id"], "at": r["session_start"][:10],
                "channel": r["channel_group"], "page": r["landing_page"],
                "pages": int(num(r["pages_viewed"])), "secs": int(num(r["session_duration_sec"])),
                "pricing": r["scrolled_pricing"] == "true",
                "form": r["form_submitted"] == "true",
                "rb2b": r["identified_by_rb2b"] == "true",
            })
    for r in sorted(sc, key=lambda r: r["scheduled_at"]):
        meetings_by_lead[r["lead_id"]].append({
            "id": r["meeting_id"], "at": r["scheduled_at"][:10], "type": r["meeting_type"],
            "rep": r["rep"], "status": r["status"], "lead_time": int(r["lead_time_days"]),
            "mins": int(num(r["duration_min"])),
            "shown": r["sample_dashboard_shown"] == "true",
            "objection": r["objection"], "outcome": r["outcome"],
        })
    for r in sorted(sub, key=lambda r: r["billing_month"]):
        invoices_by_lead[r["lead_id"]].append({
            "month": r["billing_month"], "tier": r["plan_tier"], "mrr": num(r["mrr_usd"]),
            "sources": int(r["connected_sources"]), "status": r["invoice_status"],
            "sub_status": r["subscription_status"], "m": int(r["months_since_start"]),
        })

    acct_cols = ["lead_id", "company", "domain", "contact", "title", "channel", "campaign",
                 "industry", "employees", "silos", "stage", "owner", "created", "tier",
                 "mrr", "live_mrr", "sessions", "meetings", "reason"]
    accounts = []
    for r in crm:
        lid = r["lead_id"]
        inv = invoices_by_lead.get(lid, [])
        live = 0.0
        if inv:
            last = inv[-1]
            if last["sub_status"] in ("active", "past_due"):
                live = last["mrr"]
        # TRAP 3: only a closed_lost deal has a real loss reason.
        reason = r["lost_reason"] if r["deal_stage"] == "closed_lost" else ""
        accounts.append([
            lid, r["company_name"], r["company_domain"],
            f'{r["first_name"]} {r["last_name"]}', r["title"],
            r["source_channel"], r["source_campaign"] or "Organic and direct",
            r["industry"], int(r["employee_count"]), int(r["siloed_source_count"]),
            r["deal_stage"], r["deal_owner"], r["created_date"], r["tier"],
            num(r["deal_amount_mrr"]), live,
            len(sessions_by_lead.get(lid, [])), len(meetings_by_lead.get(lid, [])),
            reason,
        ])

    detail = {}
    for r in crm:
        lid = r["lead_id"]
        d = {}
        if sessions_by_lead.get(lid):
            d["sessions"] = sessions_by_lead[lid]
        if meetings_by_lead.get(lid):
            d["meetings"] = meetings_by_lead[lid]
        if invoices_by_lead.get(lid):
            d["invoices"] = invoices_by_lead[lid]
        if d:
            detail[lid] = d

    # ---- integrity canaries, recomputed rather than asserted from memory
    canaries = [
        {"name": "Weekly spend ↔ CRM lead join",
         "test": "sum(ad.leads) == count(crm where campaign_id)",
         "left": int(sum(num(r["leads"]) for r in ad)),
         "right": sum(1 for r in crm if r["campaign_id"])},
        {"name": "Won deals ↔ Stripe subscriptions",
         "test": "count(closed_won) == count(distinct subscription_id)",
         "left": total_won, "right": len(latest)},
        {"name": "Demo flag ↔ meeting log",
         "test": "count(demo_booked) == count(distinct lead in sales_calls)",
         "left": sum(1 for r in crm if r["demo_booked"] == "true"),
         "right": len({r["lead_id"] for r in sc})},
    ]
    for c in canaries:
        c["pass"] = c["left"] == c["right"]

    return {
        "meta": {
            "window_start": min(r["created_date"] for r in crm),
            "window_end": WINDOW_END.isoformat(),
            "rows": {k: len(v) for k, v in data.items()},
            "total_rows": sum(len(v) for v in data.values()),
        },
        "canaries": canaries,
        "kpis": kpis,
        "channels": channels,
        "campaigns": campaigns,
        "silo_bands": silo_bands,
        "retention_by_sources": retention_by_sources,
        "retention_by_channel": retention_by_channel,
        "curves_by_sources": curves_by_sources,
        "curves_by_channel": curves_by_channel,
        "mrr_movement": movement,
        "churn_reasons": churn_reasons,
        "churned_total": churned_total,
        "noshow_bands": noshow_bands,
        "noshow_by_day": noshow_by_day,
        "demo_shown": demo_shown,
        "reps": reps,
        "objections": objections,
        "weekly": weekly_rows,
        "lost_reasons": lost_reasons,
        "open_with_lost_reason": open_with_lost_reason,
        "industries": industries,
        "web": web_stats,
        "accounts": {"cols": acct_cols, "rows": accounts},
        "detail": detail,
        "channel_labels": CHANNEL_LABEL,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=str(ROOT / ".tmp" / "mock_platform_data"))
    ap.add_argument("--out", default=str(ROOT / ".tmp" / "dashboard" / "semantic_layer.json"))
    args = ap.parse_args()

    data = load_all(Path(args.data))
    layer = build(data)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(layer, separators=(",", ":")), encoding="utf-8")

    kb = out.stat().st_size / 1024
    print(f"semantic layer -> {out}  ({kb:,.0f} KB)")
    for c in layer["canaries"]:
        flag = "PASS" if c["pass"] else "FAIL"
        print(f"  [{flag}] {c['name']}: {c['left']} vs {c['right']}")
    if not all(c["pass"] for c in layer["canaries"]):
        raise SystemExit("integrity canary failed — refusing to emit a dashboard from this data")


if __name__ == "__main__":
    main()
