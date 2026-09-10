"""
Layer 3 execution tool: generate every email variant against contrasting leads, for approval.

The approval step before a batch goes out. It writes each variant in
execution/visitor_identification/email_variants.json against several deliberately different leads, so you compare
approaches on real data rather than on a description of them — then set the ones you like
as `active_variants` in rb2b_config.json and run the pipeline.

Every variant is written for every chosen lead, deliberately bypassing the pipeline's
variant-per-lead routing: the point here is to judge each variant fairly, not to see what
routing would have picked.

Leads are chosen to stress different situations unless you name them:
  - one that already owns a BI tool (does the variant avoid selling them a second one?)
  - one with a big stack and no BI tool (is the manual-reporting pain landing?)
  - the smallest company (does the variant stay proportional?)

CLI usage:
    python execution/visitor_identification/generate_email_variants.py --run-name demo
    python execution/visitor_identification/generate_email_variants.py --run-name demo --leads ledgerline.com,alloyandoak.com
    python execution/visitor_identification/generate_email_variants.py --run-name demo --variants stack_math,ultra_short
    python execution/visitor_identification/generate_email_variants.py --run-name demo --dry-run
"""

import argparse
import html
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)
from spend_gate import add_spend_argument, confirm_spend  # noqa: E402  (lives in shared/)

from build_leads_dashboard import to_artifact_fragment  # noqa: E402
from generate_ad_creatives import load_brand  # noqa: E402
from generate_outreach_emails import (  # noqa: E402
    DEFAULT_MODEL,
    all_known_tools,
    build_prompt,
    generate_for_lead,
    load_frameworks,
    load_variants,
)


def choose_leads(leads, count=3):
    """Pick contrasting leads so a variant is judged against the cases that break it."""
    picked, seen = [], set()

    def take(candidate):
        if candidate and candidate["company_domain"] not in seen:
            seen.add(candidate["company_domain"])
            picked.append(candidate)

    with_bi = [l for l in leads if l.get("bi_tool")]
    take(max(with_bi, key=lambda l: l["lead_score"]) if with_bi else None)

    big_no_bi = [l for l in leads if not l.get("bi_tool") and l.get("siloed_source_count", 0) >= 5]
    take(max(big_no_bi, key=lambda l: l["lead_score"]) if big_no_bi else None)

    small = [l for l in leads if l.get("employee_count")]
    take(min(small, key=lambda l: l["employee_count"]) if small else None)

    for lead in sorted(leads, key=lambda l: -l["lead_score"]):
        if len(picked) >= count:
            break
        take(lead)
    return picked[:count]


CSS = """
:root{
  --ink:__INK__; --teal:__TEAL__; --gold:__GOLD__; --coral:__CORAL__; --navy:__NAVY__;
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
  font-family:'IBM Plex Sans',-apple-system,BlinkMacSystemFont,sans-serif;
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:0 24px}
h1{font-family:'Fraunces',Georgia,serif;font-size:31px;letter-spacing:-.02em;margin:0;
  text-wrap:balance}
h2{font-family:'Fraunces',Georgia,serif;font-size:22px;margin:0;letter-spacing:-.01em}
.eyebrow{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;
  letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.bar{position:sticky;top:0;z-index:20;background:var(--page-bg);
  border-bottom:1px solid var(--border);padding:15px 0}
.bar .inner{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.bar .mark{display:flex;align-items:center;gap:9px}
.bar .mark svg{width:22px;height:22px}
.bar .w1{font-family:'Fraunces',serif;font-weight:700;font-size:17px}
.bar .w2{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--muted)}
.tbtn{border:1px solid var(--border);background:var(--panel);color:var(--page-ink);
  border-radius:8px;padding:7px 13px;font-size:13px;cursor:pointer;font-family:inherit}
.tbtn:hover{border-color:var(--muted)}
.tbtn:focus-visible{outline:2px solid var(--teal);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
.notice{display:inline-flex;gap:8px;background:var(--chip);border:1px solid var(--border);
  border-radius:999px;padding:6px 14px;font-size:12.5px;color:var(--muted);margin-top:14px}
.notice b{color:var(--page-ink);font-weight:600}
.lede{max-width:64ch;color:var(--muted);margin:16px 0 0;font-size:16px}

.who-row{display:grid;grid-template-columns:repeat(__NLEADS__,1fr);gap:18px;margin:30px 0 8px}
.who{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.who .n{font-weight:600}
.who .m{color:var(--muted);font-size:12.5px;margin-top:3px}
.who .s{display:flex;flex-wrap:wrap;gap:5px;margin-top:9px}
.tag{background:var(--chip);border:1px solid var(--border);border-radius:999px;
  padding:2px 9px;font-size:11px;color:var(--muted)}
.tag.bi{border-color:var(--gold);color:var(--gold)}

.variant{border-top:1px solid var(--border);padding:34px 0 6px}
.vhead{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.vid{font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--teal);
  background:var(--chip);border-radius:6px;padding:3px 9px}
.vmove{color:var(--muted);font-size:14px;max-width:80ch;margin:11px 0 0}
.vuse{font-size:13px;color:var(--muted);margin-top:6px;font-style:italic}
.row{display:grid;grid-template-columns:repeat(__NLEADS__,1fr);gap:18px;margin-top:20px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;
  padding:18px;display:flex;flex-direction:column;gap:11px}
.card.flag{border-color:var(--coral)}
.card .to{font-size:11.5px;color:var(--muted);font-family:'IBM Plex Mono',monospace}
.card .subj{font-weight:600;font-size:14.5px;padding-bottom:10px;
  border-bottom:1px solid var(--border)}
.card pre{margin:0;white-space:pre-wrap;font-family:inherit;font-size:14px;line-height:1.6;
  flex:1}
.meta{display:flex;justify-content:space-between;align-items:center;gap:10px;
  font-size:11.5px;color:var(--muted);border-top:1px solid var(--border);padding-top:10px}
.warn{background:var(--warn-bg);color:var(--warn-ink);border-radius:8px;padding:9px 12px;
  font-size:12.5px}
footer{color:var(--muted);font-size:12.5px;padding:36px 0 56px;line-height:1.7;
  border-top:1px solid var(--border);margin-top:34px}
@media (max-width:900px){.row,.who-row{grid-template-columns:1fr}}
"""

SCRIPT = """
(function(){
  document.querySelectorAll('.copy').forEach(function(b){
    b.addEventListener('click', function(){
      navigator.clipboard.writeText(b.getAttribute('data-body')).then(function(){
        var was = b.textContent; b.textContent = 'Copied';
        setTimeout(function(){ b.textContent = was; }, 1300);
      });
    });
  });
  document.getElementById('theme').addEventListener('click', function(){
    var r = document.documentElement;
    r.setAttribute('data-theme', r.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
  });
})();
"""


def esc(v):
    return html.escape(str(v if v is not None else "—"))


def build_review(rows, leads, variants, brand, run_name):
    palette = brand["palette"]
    css = (
        CSS.replace("__INK__", brand["ink"]).replace("__TEAL__", palette["teal"])
        .replace("__GOLD__", palette["gold"]).replace("__CORAL__", palette["coral"])
        .replace("__NAVY__", palette["navy"]).replace("__NLEADS__", str(len(leads)))
    )
    mark = brand["mark_svg"].replace("INK", "currentColor")

    who_cards = "".join(
        f"""<div class="who"><div class="n">{esc(l['full_name'])}</div>
        <div class="m">{esc(l.get('title'))} · {esc(l['company_name'])} · {esc(l.get('employee_count'))} staff</div>
        <div class="s">{''.join(f'<span class="tag">{esc(t)}</span>' for t in (l.get('named_tools') or []))}
        {f'<span class="tag bi">{esc(l["bi_tool"])}</span>' if l.get('bi_tool') else ''}</div></div>"""
        for l in leads
    )

    by_variant = {}
    for row in rows:
        by_variant.setdefault(row["variant_id"], []).append(row)

    sections = []
    for variant in variants:
        entries = by_variant.get(variant["id"], [])
        if not entries:
            continue
        order = {l["company_domain"]: i for i, l in enumerate(leads)}
        entries.sort(key=lambda r: order.get(r["company_domain"], 99))

        cards = []
        for row in entries:
            draft = row.get("draft") or {}
            problems = row.get("problems") or []
            warn = (
                f'<div class="warn">{esc("; ".join(problems))}</div>' if problems else ""
            )
            body = draft.get("body") or "(generation failed)"
            cards.append(f"""<div class="card{' flag' if problems else ''}">
              <div class="to">to {esc(row['first_name'])} at {esc(row['company_name'])}</div>
              <div class="subj">{esc(draft.get('subject'))}</div>
              {warn}
              <pre>{esc(body)}</pre>
              <div class="meta"><span>{row.get('word_count', 0)} words</span>
                <button class="tbtn copy" data-body="{html.escape(body, quote=True)}">Copy</button>
              </div>
            </div>""")

        sections.append(f"""<div class="variant">
          <div class="vhead"><h2>{esc(variant['name'])}</h2>
            <span class="vid">{esc(variant['id'])}</span></div>
          <p class="vmove">{esc(variant['pain_move'])}</p>
          <p class="vuse">{esc(variant.get('when_to_use'))}</p>
          <div class="row">{''.join(cards)}</div>
        </div>""")

    flagged = sum(1 for r in rows if r.get("problems"))
    words = [r.get("word_count", 0) for r in rows if r.get("word_count")]
    avg = round(sum(words) / len(words)) if words else 0

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Outreach Copy Variants</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
<div class="bar"><div class="wrap"><div class="inner">
  <div class="mark" style="color:{brand['ink']}">{mark}
    <span class="w1">{esc(brand['mark_word_1'])}</span>
    <span class="w2">{esc(brand['mark_word_2'])}</span></div>
  <div style="font-size:13px;color:var(--muted)">
    {len(variants)} variants · {len(leads)} leads · {len(rows)} drafts ·
    avg {avg} words · {flagged} flagged</div>
  <button class="tbtn" id="theme">Theme</button>
</div></div></div>

<div class="wrap">
  <div style="margin-top:32px">
    <div class="eyebrow">Approval · run "{esc(run_name)}"</div>
    <h1>Pick the copy that sounds like you wrote it</h1>
    <p class="lede">Each variant below is the same offer with a different move on the pain —
      the line the email lives or dies on. Every one is written against all three leads, so
      you can see how it holds up when the prospect changes. Tell me which to keep and I'll
      set them as the active variants and re-run the pipeline.</p>
    <div class="notice"><b>Mock data</b> · fictional people and companies, nothing sent</div>
  </div>

  <div class="eyebrow" style="margin-top:34px">The three leads, left to right</div>
  <div class="who-row">{who_cards}</div>

  {''.join(sections)}

  <footer>
    Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by
    execution/visitor_identification/generate_email_variants.py, following the blend in
    execution/shared/sales_frameworks/15_warm_visitor_hybrid.md.<br>
    Edit a variant's pain move in execution/visitor_identification/email_variants.json; set the keepers as
    <strong>active_variants</strong> in execution/visitor_identification/rb2b_config.json.
  </footer>
</div>
<script>{SCRIPT}</script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description="Generate the email variant matrix for approval")
    ap.add_argument("--run-name", default="demo")
    ap.add_argument("--leads", help="comma-separated company domains; default picks 3 contrasting")
    ap.add_argument("--lead-count", type=int, default=3)
    ap.add_argument("--variants", help="comma-separated variant ids; default all")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out")
    ap.add_argument("--artifact-out")
    ap.add_argument("--dry-run", action="store_true", help="write prompts, spend nothing")
    add_spend_argument(ap)
    args = ap.parse_args()

    from rb2b_site import load_config, run_dir

    base = run_dir(args.run_name)
    enriched_path = base / "enriched.json"
    if not enriched_path.exists():
        print(f"ERROR: no enriched leads at {enriched_path}", flush=True)
        return 1

    all_leads = json.loads(enriched_path.read_text())
    if args.leads:
        wanted = {w.strip().lower() for w in args.leads.split(",")}
        leads = [l for l in all_leads if (l.get("company_domain") or "").lower() in wanted]
        if not leads:
            print(f"ERROR: no leads matched {sorted(wanted)}", flush=True)
            return 1
    else:
        leads = choose_leads(all_leads, args.lead_count)

    try:
        variants = load_variants(args.variants)
    except ValueError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1

    frameworks = load_frameworks()
    known_tools = all_known_tools()
    outdir = Path(args.out).parent if args.out else base / "variants"
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"LEADS={', '.join(l['company_name'] for l in leads)}", flush=True)
    print(f"VARIANTS={', '.join(v['id'] for v in variants)}", flush=True)
    print(f"DRAFTS_TO_GENERATE={len(leads) * len(variants)}", flush=True)

    if args.dry_run:
        prompt_dir = outdir / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        for variant in variants:
            for lead in leads:
                name = f"{variant['id']}__{lead['company_domain']}.txt"
                (prompt_dir / name).write_text(build_prompt(lead, frameworks, variant))
        print(f"  [dry-run] wrote {len(leads) * len(variants)} prompts to {prompt_dir}", flush=True)
        print("GENERATED=0", flush=True)
        return 0

    # The matrix is every variant against every lead, so the call count is the product and
    # grows faster than it looks: 6 variants x 3 leads is 18 paid calls, not 6.
    if not confirm_spend(calls=len(leads) * len(variants), model=args.model,
                         label="generate the email variant matrix",
                         assume_yes=args.yes_spend):
        print("GENERATED=0", flush=True)
        return 1

    rows = []
    for variant in variants:
        for lead in leads:
            draft, problems, attempts = generate_for_lead(
                lead, frameworks, known_tools, variant, model=args.model
            )
            body = (draft or {}).get("body") or ""
            rows.append({
                "variant_id": variant["id"],
                "variant_name": variant["name"],
                "company_name": lead["company_name"],
                "company_domain": lead["company_domain"],
                "first_name": lead["first_name"],
                "segment": lead.get("segment"),
                "draft": draft,
                "word_count": len(body.split()),
                "problems": problems,
                "attempts": attempts,
            })
            flag = "  FLAGGED" if problems else ""
            print(
                f"  [{variant['id']:<15}] {lead['company_name']:<22} "
                f"{len(body.split()):>3}w{flag}",
                flush=True,
            )

    matrix_path = outdir / "variants_matrix.json"
    matrix_path.write_text(json.dumps(rows, indent=2))

    brand = load_brand(ROOT / load_config()["brand_config"])
    page = build_review(rows, leads, variants, brand, args.run_name)
    out_path = Path(args.out) if args.out else outdir / "variants_review.html"
    out_path.write_text(page)

    if args.artifact_out:
        artifact_path = Path(args.artifact_out).expanduser()
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(to_artifact_fragment(page))
        print(f"ARTIFACT_PATH={artifact_path}", flush=True)

    flagged = sum(1 for r in rows if r["problems"])
    print(f"GENERATED={len(rows)}", flush=True)
    print(f"FLAGGED={flagged}", flush=True)
    print(f"MATRIX_PATH={matrix_path}", flush=True)
    print(f"REVIEW_PATH={out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
