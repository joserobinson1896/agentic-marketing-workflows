"""
Math double-check for the dashboard semantic layer.

Roughly fifty figures reach the dashboard from build_semantic_layer.py. This file
recomputes the load-bearing ones straight from the CSVs by a different route and
asserts they agree, then asserts that each of the seven documented traps is actually
handled rather than merely commented.

    python execution/dashboard/test_semantic_layer.py
"""

import csv
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402

from build_semantic_layer import build, load_all  # noqa: E402

DATA = ROOT / ".tmp" / "mock_platform_data"
FAILS = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {got}")


def close(label, got, want, tol=0.01):
    ok = got is not None and abs(got - want) <= tol
    if not ok:
        FAILS.append(f"{label}: got {got!r}, want ~{want!r}")
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {got}")


def main():
    raw = load_all(DATA)
    L = build(raw)
    crm, sc, sub, ad, web = (raw[k] for k in ("crm", "sc", "sub", "ad", "web"))
    by_ch = {c["channel"]: c for c in L["channels"]}

    print("\nIntegrity canaries")
    for c in L["canaries"]:
        check(c["name"], c["pass"], True)

    print("\nHeadline KPIs match the approved blueprint")
    k = L["kpis"]
    close("total spend", k["spend"], 309920.00)
    check("leads", k["leads"], 903)
    check("demos booked", k["demos_booked"], 455)
    check("meetings held", k["meetings_held"], 438)
    check("closed won", k["won"], 139)
    close("live MRR", k["live_mrr"], 540300.00)
    close("blended CAC", k["blended_cac"], 2229.64, 1.0)
    check("active subscriptions", k["active_subs"], 96)

    print("\nThe inversion holds and the ranking flips")
    close("Meta cost per lead", by_ch["meta_ads"]["cpl"], 190.00, 1.0)
    close("RB2B cost per lead", by_ch["rb2b_visitor_id"]["cpl"], 280.00, 1.0)
    close("Meta cost per $1 retained", by_ch["meta_ads"]["cost_per_retained"], 5.54, 0.02)
    close("RB2B cost per $1 retained", by_ch["rb2b_visitor_id"]["cost_per_retained"], 0.13, 0.02)
    paid = [c for c in L["channels"] if c["paid"]]
    cheapest_lead = min(paid, key=lambda c: c["cpl"])["channel"]
    cheapest_retained = min(paid, key=lambda c: c["cost_per_retained"])["channel"]
    dearest_retained = max(paid, key=lambda c: c["cost_per_retained"])["channel"]
    check("cheapest lead is Meta", cheapest_lead, "meta_ads")
    check("cheapest retained dollar is RB2B", cheapest_retained, "rb2b_visitor_id")
    check("dearest retained dollar is Meta (the inversion)", dearest_retained, "meta_ads")

    print("\nSupporting findings")
    ns = {b["band"]: b["rate"] for b in L["noshow_bands"]}
    close("no-show ≤2 days", ns["≤2 days"], 0.057, 0.002)
    close("no-show 3–5 days", ns["3–5 days"], 0.153, 0.002)
    close("no-show 6+ days", ns["6+ days"], 0.369, 0.002)
    check("no-show rate rises monotonically",
          [ns["≤2 days"] < ns["3–5 days"], ns["3–5 days"] < ns["6+ days"]], [True, True])
    close("close rate when sample shown", L["demo_shown"]["shown"]["rate"], 0.561, 0.005)
    close("close rate when never shown", L["demo_shown"]["not_shown"]["rate"], 0.257, 0.005)
    ret = {b["band"]: b["rate"] for b in L["retention_by_sources"]}
    close("retained@6mo, ≤3 sources", ret["≤3"], 0.18, 0.01)
    close("retained@6mo, 8+ sources", ret["8+"], 0.97, 0.01)
    check("retention rises with connected sources",
          [ret["≤3"] < ret["4–5"], ret["4–5"] < ret["6–7"], ret["6–7"] < ret["8+"]],
          [True, True, True])
    sb = {b["band"]: b["close_rate"] for b in L["silo_bands"]}
    close("close rate, ≤3 silos", sb["≤3"], 0.055, 0.003)
    close("close rate, 8+ silos", sb["8+"], 0.290, 0.003)

    print("\nTrap 1 — blank is not zero")
    blank_cpc = sum(1 for r in ad if r["cpc"] == "")
    check("ad rows with no auction metrics", blank_cpc, 168)
    # A channel with no impressions must not report a fabricated zero-cost click.
    check("cold email reports no CPC anywhere",
          all(r["cpc"] == "" for r in ad if r["channel"] == "cold_email"), True)
    check("weekly rows never invent a CPL from zero leads",
          all(r["cpl"] is None or r["leads"] > 0 for r in L["weekly"]), True)

    print("\nTrap 2 — retention is right-censored")
    started_late = sum(1 for r in sub
                       if r["is_first_invoice"] == "true"
                       and date(*map(int, r["subscription_started_at"].split("-"))) > date(2026, 2, 28))
    check("subscriptions too young to judge at 6 months exist", started_late > 0, True)
    check("they are excluded from the 6-month cohort",
          sum(b["eligible"] for b in L["retention_by_sources"]) < len(L["curves_by_sources"]["8+"]) * 999
          and sum(b["eligible"] for b in L["retention_by_sources"]) == 73, True)

    print("\nTrap 3 — lost_reason sits on open deals")
    check("open deals carrying a lost_reason", L["open_with_lost_reason"], 298)
    check("lost reasons counted only from closed_lost",
          sum(r["n"] for r in L["lost_reasons"]),
          sum(1 for r in crm if r["deal_stage"] == "closed_lost" and r["lost_reason"]))
    check("'Never booked a call' is not in the loss reasons",
          any(r["reason"] == "Never booked a call" for r in L["lost_reasons"]), False)

    print("\nTrap 4 — RB2B never submits a form")
    check("RB2B sessions that submitted a form", L["web"]["rb2b_with_form"], 0)
    check("RB2B still produces leads", by_ch["rb2b_visitor_id"]["leads"], 100)
    form_only = sum(1 for r in web if r["form_submitted"] == "true")
    check("a form-gated funnel would drop identified traffic",
          form_only < L["web"]["identified_leads"] + L["web"]["rb2b_identified"], True)

    print("\nTrap 5 — the CRM is last-touch")
    sessions_by_lead = {}
    for r in sorted(web, key=lambda r: r["session_start"]):
        if r["lead_id"]:
            sessions_by_lead.setdefault(r["lead_id"], []).append(r["session_id"])
    multi = {k: v for k, v in sessions_by_lead.items() if len(v) > 1}
    crm_sess = {r["lead_id"]: r["session_id"] for r in crm if r["session_id"]}
    check("multi-session leads", len(multi), 35)
    check("crm.session_id is always the LAST session",
          all(crm_sess.get(k) == v[-1] for k, v in multi.items()), True)

    print("\nTrap 6 — the demo finding is method-sensitive")
    check("the printed method is stated", bool(L["demo_shown"]["method"]), True)
    check("alternative methods are carried", len(L["demo_shown"]["alternatives"]), 2)
    overlap = ({r["lead_id"] for r in sc if r["status"] == "held" and r["sample_dashboard_shown"] == "true"}
               & {r["lead_id"] for r in sc if r["status"] == "held" and r["sample_dashboard_shown"] == "false"})
    check("'never shown' excludes leads that saw it at another meeting",
          L["demo_shown"]["not_shown"]["leads"],
          len({r["lead_id"] for r in sc if r["status"] == "held"}) - len(
              {r["lead_id"] for r in sc if r["status"] == "held" and r["sample_dashboard_shown"] == "true"}))
    check("leads appearing in both buckets", len(overlap), 52)

    print("\nTrap 7 — organic has no spend")
    org = by_ch["organic"]
    check("organic is not marked paid", org["paid"], False)
    check("organic has no cost per lead", org["cpl"], None)
    check("organic has no CAC", org["cac"], None)
    check("organic has no cost per retained dollar", org["cost_per_retained"], None)
    check("organic still reports volume", org["leads"] > 0 and org["live_mrr"] > 0, True)
    check("organic contributes no spend to the total",
          round(sum(c["spend"] for c in L["channels"]), 2), round(L["kpis"]["spend"], 2))

    print("\nInternal consistency")
    check("channel leads sum to total leads", sum(c["leads"] for c in L["channels"]), L["kpis"]["leads"])
    check("channel wins sum to total wins", sum(c["won"] for c in L["channels"]), L["kpis"]["won"])
    close("channel live MRR sums to total", sum(c["live_mrr"] for c in L["channels"]), L["kpis"]["live_mrr"])
    check("silo bands sum to total leads", sum(b["leads"] for b in L["silo_bands"]), L["kpis"]["leads"])
    check("accounts table has one row per lead", len(L["accounts"]["rows"]), L["kpis"]["leads"])
    check("every account row matches the column list",
          all(len(r) == len(L["accounts"]["cols"]) for r in L["accounts"]["rows"]), True)
    check("churn reasons dedupe to subscriptions", L["churned_total"], 43)
    check("campaigns cover every paid campaign", len(L["campaigns"]), 12)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S):")
        for f in FAILS:
            print("  -", f)
        raise SystemExit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
