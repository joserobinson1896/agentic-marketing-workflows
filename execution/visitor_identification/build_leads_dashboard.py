"""
Layer 3 execution tool: build the HTML review surface for a visitor identification run.

One page showing the whole chain per lead, so the pipeline is auditable end to end:

    ad angle they arrived on  ->  who RB2B identified  ->  the stack we detected
                              ->  the draft that was written from it

Follows the same conventions as the ad_creator gallery: brand CSS custom properties,
a prefers-color-scheme dark block plus a [data-theme] override, a sticky toolbar with
live counts, and a self-contained single file with no external assets.

Drafts flagged `needs_review` are surfaced at the top of their card with the reason,
never hidden — a draft that failed grounding is exactly the one a human must read.

CLI usage:
    python execution/visitor_identification/build_leads_dashboard.py --run-name demo
"""

import argparse
import html
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)

from generate_ad_creatives import load_brand  # noqa: E402

CSS = """
:root{
  --ink:__INK__; --shadow:__SHADOW__; --teal:__TEAL__; --gold:__GOLD__; --coral:__CORAL__;
  --navy:__NAVY__; --mint:__MINT__;
  --page-bg:#F5F4F0; --panel:#FFFFFF; --page-ink:#16201C; --muted:#5B6B63;
  --border:#DDD8CC; --chip:#F0EEE7; --warn-bg:#FFF4E5; --warn-ink:#8C4B12;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --page-bg:#12100E; --panel:#1B1917; --page-ink:#F2EFE8; --muted:#9C978C;
    --border:#2E2A26; --chip:#26231F; --warn-bg:#3A2A12; --warn-ink:#F5C182;
  }
}
:root[data-theme="dark"]{
  --page-bg:#12100E; --panel:#1B1917; --page-ink:#F2EFE8; --muted:#9C978C;
  --border:#2E2A26; --chip:#26231F; --warn-bg:#3A2A12; --warn-ink:#F5C182;
}
*{box-sizing:border-box}
body{margin:0;background:var(--page-bg);color:var(--page-ink);
  font-family:'IBM Plex Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:0 24px}
h1{font-family:'Fraunces',Georgia,serif;font-size:30px;letter-spacing:-.02em;margin:0}
.mono{font-family:'IBM Plex Mono',ui-monospace,monospace}
.eyebrow{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--muted)}

.bar{position:sticky;top:0;z-index:20;background:var(--page-bg);
  border-bottom:1px solid var(--border);padding:16px 0}
.bar .inner{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap}
.bar .mark{display:flex;align-items:center;gap:9px}
.bar .mark svg{width:22px;height:22px}
.bar .w1{font-family:'Fraunces',serif;font-weight:700;font-size:17px}
.bar .w2{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--muted)}
.counts{display:flex;gap:20px;font-size:13px;color:var(--muted);flex-wrap:wrap}
.counts b{color:var(--page-ink);font-size:15px;font-variant-numeric:tabular-nums}
.tbtn{border:1px solid var(--border);background:var(--panel);color:var(--page-ink);
  border-radius:8px;padding:7px 13px;font-size:13px;cursor:pointer;font-family:inherit}
.tbtn:hover{border-color:var(--muted)}
.tbtn:focus-visible,.head:focus-visible{outline:2px solid var(--teal);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}

.notice{display:inline-flex;align-items:center;gap:8px;background:var(--chip);
  border:1px solid var(--border);border-radius:999px;padding:6px 14px;font-size:12.5px;
  color:var(--muted);margin-top:14px}
.notice b{color:var(--page-ink);font-weight:600}
.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:22px 0 30px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px}
.stat .v{font-family:'Fraunces',serif;font-size:27px;line-height:1.1;
  font-variant-numeric:tabular-nums}
.stat .l{font-size:12px;color:var(--muted);margin-top:5px}

.lead{background:var(--panel);border:1px solid var(--border);border-radius:14px;
  margin-bottom:16px;overflow:hidden}
.lead.flag{border-color:var(--coral)}
.head{display:flex;align-items:flex-start;gap:16px;padding:20px 22px;cursor:pointer}
.score{flex:0 0 46px;height:46px;border-radius:11px;display:flex;align-items:center;
  justify-content:center;font-family:'Fraunces',serif;font-size:18px;color:#fff;
  font-variant-numeric:tabular-nums}
.who{flex:1;min-width:0}
.who .n{font-weight:600;font-size:17px}
.who .t{color:var(--muted);font-size:13.5px;margin-top:2px}
.who .subj{margin-top:9px;font-size:14px}
.who .subj span{color:var(--muted)}
.chev{color:var(--muted);font-size:13px;flex:0 0 auto;padding-top:4px}

.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{background:var(--chip);border:1px solid var(--border);border-radius:999px;
  padding:3px 10px;font-size:11.5px;color:var(--muted);white-space:nowrap}
.chip.src{border-color:var(--teal);color:var(--teal)}
.chip.named{background:var(--teal);border-color:var(--teal);color:#fff;font-weight:600}
.chip.bi{border-color:var(--gold);color:var(--gold)}
.chip.ad{border-color:var(--navy);color:var(--navy)}

.body{display:none;border-top:1px solid var(--border);padding:22px;
  grid-template-columns:1fr 1fr;gap:26px}
.lead.open .body{display:grid}
.body h4{margin:0 0 10px;font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--muted);font-family:'IBM Plex Mono',monospace}
.kv{font-size:13.5px;margin-bottom:6px;display:flex;gap:10px}
.kv b{color:var(--muted);font-weight:400;flex:0 0 128px}
.email{background:var(--page-bg);border:1px solid var(--border);border-radius:10px;padding:16px}
.email .s{font-weight:600;margin-bottom:12px;padding-bottom:11px;border-bottom:1px solid var(--border)}
.email pre{margin:0;white-space:pre-wrap;font-family:inherit;font-size:14px;line-height:1.62}
.warn{background:var(--warn-bg);color:var(--warn-ink);border-radius:9px;padding:11px 14px;
  font-size:13px;margin-bottom:14px}
.warn b{display:block;margin-bottom:4px}
.copy{margin-top:12px}
footer{color:var(--muted);font-size:12.5px;padding:34px 0 56px;line-height:1.7}
@media (max-width:820px){.summary{grid-template-columns:repeat(2,1fr)}.body{grid-template-columns:1fr}}
"""

SCRIPT = """
(function(){
  document.querySelectorAll('.head').forEach(function(h){
    h.addEventListener('click', function(e){
      if (e.target.closest('.copy')) return;
      h.parentElement.classList.toggle('open');
    });
  });
  document.querySelectorAll('.copy').forEach(function(b){
    b.addEventListener('click', function(e){
      e.stopPropagation();
      var t = b.getAttribute('data-body');
      navigator.clipboard.writeText(t).then(function(){
        var was = b.textContent; b.textContent = 'Copied';
        setTimeout(function(){ b.textContent = was; }, 1400);
      });
    });
  });
  var expanded = false;
  document.getElementById('toggle-all').addEventListener('click', function(){
    expanded = !expanded;
    document.querySelectorAll('.lead').forEach(function(l){ l.classList.toggle('open', expanded); });
    this.textContent = expanded ? 'Collapse all' : 'Expand all';
  });
  document.getElementById('theme').addEventListener('click', function(){
    var root = document.documentElement;
    var dark = root.getAttribute('data-theme') === 'dark';
    root.setAttribute('data-theme', dark ? 'light' : 'dark');
  });
})();
"""


def esc(value):
    return html.escape(str(value if value is not None else "—"))


def score_color(score, palette):
    if score >= 85:
        return palette["emerald"]
    if score >= 70:
        return palette["teal"]
    if score >= 55:
        return palette["gold"]
    return palette["slate"]


def render_lead(lead, draft, palette):
    stack = lead.get("tech_stack") or []
    siloed = set(lead.get("siloed_sources") or [])
    named = set(lead.get("named_tools") or [])
    attribution = lead.get("attribution") or {}

    chips = []
    if attribution.get("ad_id"):
        label = attribution["ad_id"] if attribution.get("is_paid") else attribution["ad_id"]
        chips.append(f'<span class="chip ad">{esc(label)}</span>')
    for tool in stack:
        cls = "chip named" if tool["tool"] in named else (
            "chip bi" if tool["tool"] == lead.get("bi_tool") else
            ("chip src" if tool["tool"] in siloed else "chip")
        )
        chips.append(f'<span class="{cls}">{esc(tool["tool"])}</span>')

    problems = (draft or {}).get("problems") or []
    warn = ""
    if problems:
        items = "".join(f"<div>· {esc(p)}</div>" for p in problems)
        warn = f'<div class="warn"><b>Needs review — grounding checks failed</b>{items}</div>'

    subject = (draft or {}).get("subject") or "(no draft generated)"
    body = (draft or {}).get("body") or ""

    facts = [
        ("Title", lead.get("title")),
        ("Company", lead.get("company_name")),
        ("Domain", lead.get("company_domain")),
        ("Industry", lead.get("industry")),
        ("Headcount", lead.get("employee_count")),
        ("Est. revenue", lead.get("estimated_revenue")),
        ("Location", f"{lead.get('city')}, {lead.get('state')}"),
        ("Email", lead.get("business_email")),
        ("LinkedIn", lead.get("linkedin_url")),
        ("Segment", lead.get("segment")),
        ("Data sources", lead.get("siloed_source_count")),
        ("BI tool", lead.get("bi_tool")),
    ]
    fact_html = "".join(f'<div class="kv"><b>{esc(k)}</b><span>{esc(v)}</span></div>' for k, v in facts)

    arrival = [
        ("Ad", attribution.get("ad_id")),
        ("Angle", attribution.get("angle")),
        ("Channel", attribution.get("channel")),
        ("Campaign", attribution.get("utm_campaign")),
        ("Referrer", attribution.get("referrer")),
        ("Landing page", attribution.get("landing_page")),
    ]
    arrival_html = "".join(f'<div class="kv"><b>{esc(k)}</b><span>{esc(v)}</span></div>' for k, v in arrival)

    score = lead.get("lead_score", 0)
    return f"""<div class="lead{' flag' if problems else ''}">
  <div class="head">
    <div class="score" style="background:{score_color(score, palette)}">{score}</div>
    <div class="who">
      <div class="n">{esc(lead.get('full_name'))}</div>
      <div class="t">{esc(lead.get('title'))} · {esc(lead.get('company_name'))} · {esc(lead.get('city'))}, {esc(lead.get('state'))}</div>
      <div class="subj"><span>Subject:</span> {esc(subject)}</div>
      <div class="chips">{''.join(chips)}</div>
    </div>
    <div class="chev">▾</div>
  </div>
  <div class="body">
    <div>
      <h4>Identified &amp; enriched</h4>
      {fact_html}
      <h4 style="margin-top:22px">How they arrived</h4>
      {arrival_html}
    </div>
    <div>
      <h4>Draft email</h4>
      {warn}
      <div class="email">
        <div class="s">{esc(subject)}</div>
        <pre>{esc(body)}</pre>
      </div>
      <button class="tbtn copy" data-body="{html.escape(body, quote=True)}">Copy body</button>
    </div>
  </div>
</div>"""


def build_dashboard(leads, drafts, brand, run_name, meta=None):
    palette = brand["palette"]
    by_domain = {d.get("company_domain"): d for d in drafts}
    meta = meta or {}

    total = len(leads)
    drafted = len(drafts)
    flagged = sum(1 for d in drafts if d.get("needs_review"))
    avg = round(sum(l.get("lead_score", 0) for l in leads) / total) if total else 0
    sources = sum(l.get("siloed_source_count", 0) for l in leads)

    cards = "".join(
        render_lead(lead, by_domain.get(lead.get("company_domain")), palette) for lead in leads
    )
    mark = brand["mark_svg"].replace("INK", "currentColor")
    css = (
        CSS.replace("__INK__", brand["ink"]).replace("__SHADOW__", brand["shadow_ink"])
        .replace("__TEAL__", palette["teal"]).replace("__GOLD__", palette["gold"])
        .replace("__CORAL__", palette["coral"]).replace("__NAVY__", palette["navy"])
        .replace("__MINT__", palette["mint"])
    )

    engine = meta.get("engine", "browser")
    pageviews = meta.get("pageviews", "—")
    sessions = meta.get("sessions", "—")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unified Dashboards Leads</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
<div class="bar"><div class="wrap"><div class="inner">
  <div class="mark" style="color:{brand['ink']}">{mark}
    <span class="w1">{esc(brand['mark_word_1'])}</span>
    <span class="w2">{esc(brand['mark_word_2'])}</span>
  </div>
  <div class="counts">
    <span><b>{total}</b> identified</span>
    <span><b>{drafted}</b> drafted</span>
    <span><b>{flagged}</b> need review</span>
    <span><b>{avg}</b> avg score</span>
  </div>
  <div style="display:flex;gap:9px">
    <button class="tbtn" id="toggle-all">Expand all</button>
    <button class="tbtn" id="theme">Theme</button>
  </div>
</div></div></div>

<div class="wrap">
  <div style="margin:30px 0 0">
    <div class="eyebrow">Visitor identification · run "{esc(run_name)}"</div>
    <h1>Who landed on the site, and what to send them</h1>
    <div class="notice"><b>Mock run</b> · identity resolution simulated; people, companies
      and addresses are fictional and nothing was sent</div>
  </div>

  <div class="summary">
    <div class="stat"><div class="v">{sessions}</div><div class="l">visitor sessions</div></div>
    <div class="stat"><div class="v">{pageviews}</div><div class="l">pageviews tracked</div></div>
    <div class="stat"><div class="v">{total}</div><div class="l">resolved to a person</div></div>
    <div class="stat"><div class="v">{sources}</div><div class="l">siloed data sources found</div></div>
  </div>

  {cards}

  <footer>
    <strong>Visitor identification pipeline</strong> — generated
    {datetime.now().strftime('%Y-%m-%d %H:%M')} by execution/visitor_identification/build_leads_dashboard.py.<br>
    Traffic engine: {esc(engine)}. Identity resolution was mocked by
    execution/visitor_identification/rb2b_mock_resolver.py; everything downstream of the webhook is production code.
    All people, companies, domains and email addresses are fictional and nothing was sent.<br>
    See directives/visitor_identification.md for the SOP and the RB2B cutover steps.
  </footer>
</div>
<script>{SCRIPT}</script>
</body>
</html>"""


def to_artifact_fragment(page_html):
    """Strip the document skeleton for publishing as an Artifact.

    The Artifact runtime wraps content in its own <!doctype>/<head>/<body>, so we hand it
    the title, font link, styles, body content and script — and nothing else.
    """
    def between(open_tag, close_tag, source):
        start = source.find(open_tag)
        end = source.find(close_tag, start)
        return source[start:end + len(close_tag)] if start != -1 and end != -1 else ""

    title = between("<title>", "</title>", page_html)
    font_link = between('<link href="https://fonts.googleapis.com', ">", page_html)
    styles = between("<style>", "</style>", page_html)
    body_start = page_html.find("<body>") + len("<body>")
    body = page_html[body_start:page_html.rfind("</body>")]
    return f"{title}\n{font_link}\n{styles}\n{body.strip()}\n"


def main():
    ap = argparse.ArgumentParser(description="Build the leads review dashboard")
    ap.add_argument("--run-name", default="demo")
    ap.add_argument("--enriched")
    ap.add_argument("--drafts")
    ap.add_argument("--out")
    ap.add_argument("--sessions")
    ap.add_argument("--pageviews")
    ap.add_argument("--engine")
    ap.add_argument("--artifact-out", help="also write a wrapper-ready fragment for publishing")
    args = ap.parse_args()

    from rb2b_site import load_config, run_dir

    base = run_dir(args.run_name)
    enriched_path = Path(args.enriched) if args.enriched else base / "enriched.json"
    drafts_path = Path(args.drafts) if args.drafts else base / "emails" / "drafts.json"
    out_path = Path(args.out) if args.out else base / "leads_dashboard.html"

    if not enriched_path.exists():
        print(f"ERROR: no enriched leads at {enriched_path}", flush=True)
        return 1

    leads = json.loads(enriched_path.read_text())
    drafts = json.loads(drafts_path.read_text()) if drafts_path.exists() else []
    brand = load_brand(ROOT / load_config()["brand_config"])

    meta = {k: v for k, v in (
        ("sessions", args.sessions), ("pageviews", args.pageviews), ("engine", args.engine)
    ) if v}

    page = build_dashboard(leads, drafts, brand, args.run_name, meta)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page)

    if args.artifact_out:
        artifact_path = Path(args.artifact_out).expanduser()
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(to_artifact_fragment(page))
        print(f"ARTIFACT_PATH={artifact_path}", flush=True)

    print(f"DASHBOARD_PATH={out_path}", flush=True)
    print(f"LEADS={len(leads)}", flush=True)
    print(f"DRAFTS={len(drafts)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
