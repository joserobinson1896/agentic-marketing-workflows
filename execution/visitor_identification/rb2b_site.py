"""
Layer 3 execution tool: the Unified Dashboards marketing site + the RB2B webhook receiver.

Two things live in this one file because they are two halves of the same seam:

  1. The SITE. A real Flask app serving real pages that real HTTP traffic can browse.
     It renders the RB2B tracking pixel just before </head>, which is where RB2B
     requires it. In `mode: "live"` that is RB2B's actual snippet pointed at their
     CDN; in `mode: "mock"` it is a local shim exposing the same `reb2b.load()`
     surface, beaconing pageviews to /mock/collect instead.

  2. The RECEIVER. POST /rb2b/webhook is production code written against RB2B's
     documented contract. It does not know or care whether the payload came from
     RB2B or from the mock resolver. On cutover it is not touched.

Receiver contract (verified against RB2B's docs — see directives/visitor_identification.md):
  - One JSON object per person. Never batched.
  - Keys are space-separated Title Case: "LinkedIn URL", "First Name", "Business Email"...
  - Only "LinkedIn URL" and "First Name" are guaranteed present; everything else is nullable.
  - "Employee Count" is typed integer OR string.
  - No HMAC/signature support. Auth is a random token in the URL query string.
  - RB2B disables a webhook endpoint that repeatedly times out, so this handler only
    ever validates, normalizes and appends. All slow work (enrichment, LLM drafting)
    happens later in separate batch scripts, by design.

CLI usage:
    python execution/visitor_identification/rb2b_site.py --run-name demo
    python execution/visitor_identification/rb2b_site.py --run-name demo --port 5001 --config execution/visitor_identification/rb2b_config.json

Importable:
    from rb2b_site import create_app, load_config, normalize_payload, VisitorStore
"""

import argparse
import hmac
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import FONTS, ROOT  # noqa: E402  (also puts every execution area on sys.path)

# Data that belongs to this area lives beside it.
HERE = Path(__file__).resolve().parent

from generate_ad_creatives import load_brand  # noqa: E402  (shared brand system)

DEFAULT_CONFIG_PATH = HERE / "rb2b_config.json"
FONT_DIR = FONTS


# --------------------------------------------------------------------------- config


def load_config(path=None):
    cfg = json.loads(Path(path or DEFAULT_CONFIG_PATH).read_text())
    return {k: v for k, v in cfg.items() if not k.startswith("_comment")}


def run_dir(run_name):
    return ROOT / ".tmp" / "visitors" / run_name


# ------------------------------------------------------------------- RB2B contract

# RB2B's wire keys -> our internal snake_case. Their "Estimate Revenue" wording is
# theirs, not a typo on our side; we normalize it to estimated_revenue inward.
RB2B_FIELD_MAP = {
    "LinkedIn URL": "linkedin_url",
    "First Name": "first_name",
    "Last Name": "last_name",
    "Title": "title",
    "Company Name": "company_name",
    "Business Email": "business_email",
    "Website": "website",
    "Industry": "industry",
    "Employee Count": "employee_count",
    "Estimate Revenue": "estimated_revenue",
    "City": "city",
    "State": "state",
    "Zipcode": "zipcode",
    "Seen At": "seen_at",
    "Referrer": "referrer",
    "Captured URL": "captured_url",
    "Tags": "tags",
    "is_repeat_visit": "is_repeat_visit",
}


def coerce_employee_count(value):
    """RB2B types Employee Count as integer OR string, and real data carries ranges.

    Returns (int_or_None, original). Never raises — a weird band must not cost us a lead.
    """
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, value
    if isinstance(value, int):
        return value, value
    text = str(value).strip()
    if not text:
        return None, value
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None, value
    # "201-500" and "201 to 500" -> take the low end; "250+" and "250" -> 250.
    for sep in ("-", "–", " to "):
        if sep in text:
            low = "".join(ch for ch in text.split(sep)[0] if ch.isdigit())
            return (int(low) if low else None), value
    return int(digits), value


def parse_seen_at(value):
    """RB2B's own doc example is malformed ISO ('...T12:34:56:00.00+00.00').

    Try to parse; on failure keep the raw string rather than dropping the record.
    """
    if not value:
        return None
    text = str(value).strip()
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate).astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def normalize_payload(payload):
    """Map an RB2B webhook body into our internal record.

    Unknown keys are preserved under `_unmapped` — if RB2B adds a field we want it in
    the store, not silently dropped on the floor.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    record, unmapped = {}, {}
    for key, value in payload.items():
        target = RB2B_FIELD_MAP.get(key)
        if target:
            record[target] = value
        else:
            unmapped[key] = value

    for field in RB2B_FIELD_MAP.values():
        record.setdefault(field, None)

    emp, emp_raw = coerce_employee_count(record.get("employee_count"))
    record["employee_count"] = emp
    record["employee_count_raw"] = emp_raw

    record["seen_at_raw"] = record.get("seen_at")
    record["seen_at"] = parse_seen_at(record.get("seen_at"))
    record["is_repeat_visit"] = bool(record.get("is_repeat_visit"))
    record["_unmapped"] = unmapped
    record["received_at"] = datetime.now(timezone.utc).isoformat()

    # The two fields RB2B guarantees. Everything else may legitimately be null.
    if not record.get("linkedin_url"):
        raise ValueError("missing required field: LinkedIn URL")
    if not record.get("first_name"):
        raise ValueError("missing required field: First Name")

    full = " ".join(p for p in [record.get("first_name"), record.get("last_name")] if p)
    record["full_name"] = full or record["first_name"]
    site = record.get("website") or ""
    record["company_domain"] = (
        site.replace("https://", "").replace("http://", "").strip("/").split("/")[0].lower() or None
    )
    return record


class VisitorStore:
    """Append-only JSONL of identified people, deduped on LinkedIn URL.

    RB2B fires on initial visit only unless the repeat-visitor toggle is on, but a
    receiver that trusts that is one dashboard setting away from duplicate outreach.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seen = set()
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    try:
                        self._seen.add(json.loads(line).get("linkedin_url"))
                    except json.JSONDecodeError:
                        continue

    def append(self, record):
        """Returns True if stored, False if it was a duplicate."""
        with self._lock:
            key = record.get("linkedin_url")
            if key in self._seen:
                return False
            self._seen.add(key)
            with self.path.open("a") as fh:
                fh.write(json.dumps(record) + "\n")
            return True

    def all(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def __len__(self):
        return len(self._seen)


# ------------------------------------------------------------------------ the pixel

# Standard analytics loader stub, pointed at RB2B's documented CDN path and snippet
# version. NOTE: the bytes RB2B hands you in their dashboard are minified and may
# differ; on cutover paste THEIR snippet here rather than trusting this reconstruction.
LIVE_PIXEL = """<script>
!function () {
  var reb2b = window.reb2b = window.reb2b || [];
  if (reb2b.invoked) return;
  reb2b.invoked = true;
  reb2b.methods = ["identify", "collect"];
  reb2b.factory = function (method) {
    return function () {
      var args = Array.prototype.slice.call(arguments);
      args.unshift(method);
      reb2b.push(args);
      return reb2b;
    };
  };
  for (var i = 0; i < reb2b.methods.length; i++) {
    var key = reb2b.methods[i];
    reb2b[key] = reb2b.factory(key);
  }
  reb2b.load = function (key) {
    var script = document.createElement("script");
    script.type = "text/javascript";
    script.async = true;
    script.src = "__CDN__/" + key + "/" + key + ".js.gz";
    var first = document.getElementsByTagName("script")[0];
    first.parentNode.insertBefore(script, first);
  };
  reb2b.SNIPPET_VERSION = "__VERSION__";
  reb2b.load("__SCRIPT_ID__");
}();
</script>"""

# Mock mode: structurally identical call site, local shim instead of RB2B's CDN.
MOCK_PIXEL = """<script src="/rb2b-shim.js"></script>
<script>
  reb2b.SNIPPET_VERSION = "__VERSION__";
  reb2b.load("__SCRIPT_ID__");
</script>"""

SHIM_JS = """/* Mock RB2B pixel. Same public surface as reb2b (load/identify/collect) so the
   page's call site is byte-for-byte what it will be in live mode. Instead of RB2B's
   identity graph it beacons the pageview to /mock/collect, where the mock resolver
   decides whether this visit resolves to a person. Delete on cutover. */
(function () {
  var reb2b = (window.reb2b = window.reb2b || []);
  reb2b.invoked = true;
  reb2b.methods = ["identify", "collect"];
  reb2b.factory = function (m) {
    return function () { reb2b.push([m].concat([].slice.call(arguments))); return reb2b; };
  };
  for (var i = 0; i < reb2b.methods.length; i++) {
    reb2b[reb2b.methods[i]] = reb2b.factory(reb2b.methods[i]);
  }
  reb2b.load = function (key) {
    reb2b.SCRIPT_ID = key;
    try {
      var body = JSON.stringify({
        script_id: key,
        captured_url: window.location.href,
        referrer: document.referrer || null,
        visitor_id: reb2b.visitorId(),
        title: document.title
      });
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/mock/collect", new Blob([body], { type: "application/json" }));
      } else {
        var xhr = new XMLHttpRequest();
        xhr.open("POST", "/mock/collect", true);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.send(body);
      }
    } catch (e) { /* a pixel must never break the page */ }
  };
  reb2b.visitorId = function () {
    try {
      var id = localStorage.getItem("_reb2b_vid");
      if (!id) {
        id = "v-" + Math.random().toString(36).slice(2, 12);
        localStorage.setItem("_reb2b_vid", id);
      }
      return id;
    } catch (e) { return "v-nostorage"; }
  };
})();
"""


def render_pixel(config):
    template = LIVE_PIXEL if config.get("mode") == "live" else MOCK_PIXEL
    return (
        template.replace("__CDN__", config.get("rb2b_cdn", ""))
        .replace("__VERSION__", config.get("rb2b_snippet_version", "1.0.1"))
        .replace("__SCRIPT_ID__", config.get("rb2b_script_id", "XXXXXXXXXXX"))
    )


# -------------------------------------------------------------------------- the site


def font_face_css():
    """@font-face block over the vendored woff2 files, served from /fonts/<file>."""
    manifest = json.loads((FONT_DIR / "manifest.json").read_text())
    seen, blocks = set(), []
    for face in manifest:
        key = (face["family"], face["weight"], face["style"])
        if key in seen:
            continue
        seen.add(key)
        blocks.append(
            f"@font-face{{font-family:'{face['family']}';font-weight:{face['weight']};"
            f"font-style:{face['style']};font-display:swap;"
            f"src:url('/fonts/{face['file']}') format('woff2');}}"
        )
    return "\n".join(blocks)


SITE_CSS = """
:root{
  --ink:__INK__; --shadow:__SHADOW__; --teal:__TEAL__; --gold:__GOLD__;
  --coral:__CORAL__; --navy:__NAVY__; --mint:__MINT__;
  --bg:#F7F6F2; --panel:#FFFFFF; --muted:#5B6B63; --border:#E2DED2;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:'IBM Plex Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  font-size:17px;line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:inherit}
.wrap{max-width:1080px;margin:0 auto;padding:0 28px}
h1,h2,h3{font-family:'Fraunces',Georgia,serif;font-weight:700;line-height:1.08;
  letter-spacing:-0.02em;margin:0}
.eyebrow{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;
  letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}

nav{display:flex;align-items:center;justify-content:space-between;padding:22px 0;
  border-bottom:1px solid var(--border)}
.mark{display:flex;align-items:center;gap:10px;text-decoration:none}
.mark svg{width:26px;height:26px}
.mark .w1{font-family:'Fraunces',serif;font-weight:700;font-size:20px}
.mark .w2{font-family:'IBM Plex Mono',monospace;font-size:12px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--shadow);align-self:flex-end;padding-bottom:3px}
nav .links{display:flex;gap:26px;align-items:center;font-size:15px}
nav .links a{text-decoration:none;color:var(--muted)}
nav .links a:hover{color:var(--ink)}
.btn{display:inline-block;background:var(--ink);color:#fff;text-decoration:none;
  padding:13px 22px;border-radius:9px;font-weight:600;font-size:15px;border:none;cursor:pointer}
.btn:hover{background:var(--shadow)}
.btn.ghost{background:transparent;color:var(--ink);border:1.5px solid var(--ink)}

.hero{padding:84px 0 64px;display:grid;grid-template-columns:1.05fr .95fr;gap:56px;align-items:center}
.hero h1{font-size:clamp(38px,5vw,60px)}
.hero p.lede{font-size:20px;color:var(--muted);margin:22px 0 30px;max-width:34em}
.hero .cta-row{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.hero .note{font-size:14px;color:var(--muted);margin-top:16px}

.panel{background:var(--panel);border:1px solid var(--border);border-radius:16px;
  box-shadow:0 18px 44px -28px rgba(16,26,23,.42)}
.chart{padding:22px}
.chart .row{display:flex;justify-content:space-between;align-items:center;
  padding:11px 0;border-bottom:1px solid var(--border);font-size:14px}
.chart .row:last-child{border-bottom:none}
.chart .src{display:flex;align-items:center;gap:9px;color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.bars{display:flex;align-items:flex-end;gap:7px;height:96px;padding:20px 22px 0}
.bars i{flex:1;border-radius:4px 4px 0 0;display:block}
.chart h4{font-family:'IBM Plex Sans',sans-serif;font-size:13px;font-weight:600;
  text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0;padding:20px 22px 0}

section{padding:64px 0;border-top:1px solid var(--border)}
section h2{font-size:clamp(28px,3.4vw,38px);max-width:20em}
section p.sub{color:var(--muted);font-size:18px;margin-top:14px;max-width:38em}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:22px;margin-top:38px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:26px}
.card .n{font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--muted);
  letter-spacing:.12em}
.card h3{font-size:20px;margin:12px 0 9px}
.card p{margin:0;color:var(--muted);font-size:15.5px}
.stack{display:flex;flex-wrap:wrap;gap:9px;margin-top:26px}
.chip{background:var(--panel);border:1px solid var(--border);border-radius:999px;
  padding:7px 15px;font-size:14px;color:var(--muted)}
.quote{font-family:'Fraunces',serif;font-size:26px;line-height:1.36;max-width:22em;margin:0}
.quote + .who{margin-top:18px;font-size:15px;color:var(--muted)}
.cta-band{background:var(--ink);color:#fff;border-radius:18px;padding:52px;
  display:flex;justify-content:space-between;align-items:center;gap:32px;flex-wrap:wrap}
.cta-band h2{color:#fff;font-size:32px}
.cta-band .btn{background:var(--gold);color:var(--ink)}
.cta-band .btn:hover{background:#fff}
footer{padding:38px 0 56px;color:var(--muted);font-size:14px}
.pricing{display:grid;grid-template-columns:repeat(3,1fr);gap:22px;margin-top:40px}
.tier{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:30px}
.tier.mid{border-color:var(--ink);border-width:2px}
.tier .price{font-family:'Fraunces',serif;font-size:34px;margin:14px 0 4px}
.tier ul{margin:20px 0 26px;padding-left:18px;color:var(--muted);font-size:15px}
.tier li{margin-bottom:9px}
@media (max-width:860px){
  .hero{grid-template-columns:1fr;padding:52px 0 44px}
  .grid3,.pricing{grid-template-columns:1fr}
  nav .links a:not(.btn){display:none}
  .cta-band{padding:34px}
}
"""


def render_mark(brand):
    svg = brand["mark_svg"].replace("INK", brand["ink"])
    return (
        f'<a class="mark" href="/">{svg}'
        f'<span class="w1">{brand["mark_word_1"]}</span>'
        f'<span class="w2">{brand["mark_word_2"]}</span></a>'
    )


def page_shell(title, body, config, brand):
    p = brand["palette"]
    css = (
        SITE_CSS.replace("__INK__", brand["ink"])
        .replace("__SHADOW__", brand["shadow_ink"])
        .replace("__TEAL__", p["teal"])
        .replace("__GOLD__", p["gold"])
        .replace("__CORAL__", p["coral"])
        .replace("__NAVY__", p["navy"])
        .replace("__MINT__", p["mint"])
    )
    nav = f"""<nav>{render_mark(brand)}
      <div class="links">
        <a href="/sample-dashboard">Sample dashboard</a>
        <a href="/pricing">Pricing</a>
        <a class="btn" href="/pricing">Book a walkthrough →</a>
      </div></nav>"""
    # The pixel sits immediately before </head> — RB2B's documented requirement.
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{font_face_css()}</style>
<style>{css}</style>
{render_pixel(config)}
</head>
<body>
<div class="wrap">{nav}</div>
{body}
<div class="wrap"><footer>
  <strong>Unified Dashboards</strong> — one dashboard built on the tools you already run.<br>
  Demo site for the visitor identification pipeline. See directives/visitor_identification.md.
</footer></div>
</body>
</html>"""


def _sample_panel(brand):
    p = brand["palette"]
    rows = [
        ("HubSpot", "Pipeline", "$1.24M", p["teal"]),
        ("Stripe", "MRR", "$182,400", p["navy"]),
        ("Mailchimp", "Sends → signups", "3.1%", p["gold"]),
        ("Google Analytics", "Paid sessions", "48,910", p["coral"]),
    ]
    row_html = "".join(
        f'<div class="row"><span class="src"><i class="dot" style="background:{c}"></i>'
        f"{tool} · {metric}</span><strong>{val}</strong></div>"
        for tool, metric, val, c in rows
    )
    heights = [38, 52, 44, 68, 59, 81, 72, 92]
    bars = "".join(
        f'<i style="height:{h}%;background:{p["teal"] if i % 2 else p["navy"]};opacity:{.45 + i * .07:.2f}"></i>'
        for i, h in enumerate(heights)
    )
    return f"""<div class="panel">
      <h4>Revenue, all sources · last 30 days</h4>
      <div class="bars">{bars}</div>
      <div class="chart">{row_html}</div>
    </div>"""


def page_home(config, brand):
    body = f"""
<div class="wrap">
  <div class="hero">
    <div>
      <div class="eyebrow">Done-for-you reporting</div>
      <h1>Your numbers live in six tools. Your board wants one screen.</h1>
      <p class="lede">We build and maintain a single dashboard on top of the stack you already run —
        HubSpot, Stripe, Mailchimp, GA4, whatever it is. You stop rebuilding the same report every
        Monday, and everyone finally argues about the strategy instead of the numbers.</p>
      <div class="cta-row">
        <a class="btn" href="/pricing">Book a walkthrough →</a>
        <a class="btn ghost" href="/sample-dashboard">See a sample dashboard</a>
      </div>
      <p class="note">Built for you in 14 days. No engineering time from your side.</p>
    </div>
    {_sample_panel(brand)}
  </div>
</div>

<div class="wrap"><section>
  <div class="eyebrow">The problem</div>
  <h2>Nobody in the meeting believes the number on the slide.</h2>
  <p class="sub">Every tool reports on itself and none of them agree. Someone exports four CSVs,
    stitches them in a spreadsheet by hand, and by the time it's ready the question has changed.</p>
  <div class="grid3">
    <div class="card"><div class="n">01</div><h3>Four tools, four truths</h3>
      <p>Your CRM, your billing, your ESP and your analytics each count a customer differently.
        No one owns the reconciliation.</p></div>
    <div class="card"><div class="n">02</div><h3>The Monday scramble</h3>
      <p>Eight to twelve hours a week rebuilding the same report, on someone whose job is
        supposed to be growth.</p></div>
    <div class="card"><div class="n">03</div><h3>Revenue you can't attribute</h3>
      <p>Paid spend that clearly worked, sitting in a channel your reports credit to "direct".</p></div>
  </div>
</section></div>

<div class="wrap"><section>
  <div class="eyebrow">How it works</div>
  <h2>We connect what you have. You get one screen that updates itself.</h2>
  <div class="grid3">
    <div class="card"><div class="n">STEP 1</div><h3>Stack audit</h3>
      <p>We map every system that touches revenue and agree on the definitions — what counts
        as a lead, an opportunity, a customer.</p></div>
    <div class="card"><div class="n">STEP 2</div><h3>Build in 14 days</h3>
      <p>We pipe your sources into one warehouse and build the dashboard. No engineering
        time from your team.</p></div>
    <div class="card"><div class="n">STEP 3</div><h3>We keep it running</h3>
      <p>Schemas change, tools get swapped, someone renames a field. That's on us, not you.</p></div>
  </div>
  <div class="stack">
    <span class="chip">HubSpot</span><span class="chip">Salesforce</span><span class="chip">Stripe</span>
    <span class="chip">Mailchimp</span><span class="chip">Klaviyo</span><span class="chip">GA4</span>
    <span class="chip">Segment</span><span class="chip">Shopify</span><span class="chip">Snowflake</span>
    <span class="chip">Looker</span><span class="chip">Meta Ads</span><span class="chip">Google Ads</span>
  </div>
</section></div>

<div class="wrap"><section>
  <p class="quote">"We cut reporting from eleven hours a week to about twenty minutes. The bigger
    thing is that nobody argues about whose number is right any more."</p>
  <p class="who">VP Marketing, 240-person logistics company</p>
</section></div>

<div class="wrap"><section style="border:none">
  <div class="cta-band">
    <h2>See what yours would look like.</h2>
    <a class="btn" href="/pricing">Book a walkthrough →</a>
  </div>
</section></div>
"""
    return page_shell("Unified Dashboards — one dashboard for every tool you run", body, config, brand)


def page_sample(config, brand):
    body = f"""
<div class="wrap"><section style="border:none;padding-top:52px">
  <div class="eyebrow">Sample dashboard</div>
  <h2>This is a real build, with the client's numbers changed.</h2>
  <p class="sub">One screen, four systems behind it. Updates every morning without anyone
    touching a spreadsheet.</p>
  <div style="margin-top:34px">{_sample_panel(brand)}</div>
  <div class="grid3">
    <div class="card"><div class="n">SOURCES</div><h3>6 systems</h3>
      <p>HubSpot, Stripe, Mailchimp, GA4, Google Ads and a Postgres app database.</p></div>
    <div class="card"><div class="n">REFRESH</div><h3>Every morning</h3>
      <p>Landed before the 9am standup. Failures alert us, not you.</p></div>
    <div class="card"><div class="n">TIME BACK</div><h3>11 hrs → 20 min</h3>
      <p>What the marketing team used to spend assembling this by hand each week.</p></div>
  </div>
  <div style="margin-top:40px"><a class="btn" href="/pricing">Book a walkthrough →</a></div>
</section></div>
"""
    return page_shell("Sample dashboard — Unified Dashboards", body, config, brand)


def page_pricing(config, brand):
    body = """
<div class="wrap"><section style="border:none;padding-top:52px">
  <div class="eyebrow">Pricing</div>
  <h2>Built for you, then maintained. No seats to buy.</h2>
  <p class="sub">Every engagement starts with a walkthrough of your stack. We'll tell you
    honestly if you don't need us yet.</p>
  <div class="pricing">
    <div class="tier"><div class="eyebrow">Starter</div><div class="price">$2,400<span
      style="font-size:15px;color:var(--muted)">/mo</span></div>
      <ul><li>Up to 4 connected sources</li><li>One executive dashboard</li>
        <li>Daily refresh</li><li>Email support</li></ul>
      <a class="btn ghost" href="#">Book a walkthrough →</a></div>
    <div class="tier mid"><div class="eyebrow">Growth · most common</div><div class="price">$4,800<span
      style="font-size:15px;color:var(--muted)">/mo</span></div>
      <ul><li>Up to 10 connected sources</li><li>Executive + channel dashboards</li>
        <li>Warehouse included</li><li>Attribution modelling</li><li>Shared Slack channel</li></ul>
      <a class="btn" href="#">Book a walkthrough →</a></div>
    <div class="tier"><div class="eyebrow">Enterprise</div><div class="price">Custom</div>
      <ul><li>Unlimited sources</li><li>Your warehouse or ours</li>
        <li>Custom data models</li><li>Named analyst</li></ul>
      <a class="btn ghost" href="#">Talk to us →</a></div>
  </div>
</section></div>
"""
    return page_shell("Pricing — Unified Dashboards", body, config, brand)


# --------------------------------------------------------------------------- the app


def create_app(config, store, on_pageview=None):
    """Build the Flask app.

    `on_pageview(event) -> None` is the mock resolver hook. In live mode it is None and
    /mock/collect 404s, because RB2B's own script is doing that job from their side.
    """
    from flask import Flask, Response, abort, jsonify, request, send_from_directory

    brand = load_brand(ROOT / config["brand_config"])
    app = Flask(__name__)
    app.config["RB2B"] = config
    app.config["STORE"] = store

    @app.get("/")
    def home():
        return page_home(config, brand)

    @app.get("/sample-dashboard")
    def sample():
        return page_sample(config, brand)

    @app.get("/pricing")
    def pricing():
        return page_pricing(config, brand)

    @app.get("/fonts/<path:filename>")
    def fonts(filename):
        return send_from_directory(FONT_DIR, filename, max_age=86400)

    @app.get("/rb2b-shim.js")
    def shim():
        if config.get("mode") == "live":
            abort(404)  # live mode loads RB2B's real script from their CDN
        return Response(SHIM_JS, mimetype="application/javascript")

    @app.post("/mock/collect")
    def collect():
        """Pageview beacon from the shim. Mock-mode only — deleted on cutover."""
        if config.get("mode") == "live" or on_pageview is None:
            abort(404)
        event = request.get_json(silent=True) or {}
        event["ip"] = request.headers.get("X-Forwarded-For", request.remote_addr)
        event["user_agent"] = request.headers.get("User-Agent")
        event["received_at"] = datetime.now(timezone.utc).isoformat()
        on_pageview(event)
        return "", 204

    @app.post("/rb2b/webhook")
    def webhook():
        """PRODUCTION receiver. Untouched on cutover to a real RB2B account.

        Validates, normalizes, appends, acks. Deliberately does no enrichment or LLM
        work inline: RB2B disables endpoints that time out, so everything slow runs
        later as a batch over the JSONL this writes.
        """
        expected = config.get("webhook_token") or ""
        supplied = request.args.get("token", "")
        # RB2B offers no signature/HMAC, so a URL token is the documented mechanism.
        if not expected or not hmac.compare_digest(supplied, expected):
            return jsonify({"error": "invalid token"}), 401

        payload = request.get_json(silent=True)
        try:
            record = normalize_payload(payload)
        except ValueError as exc:
            # 400, not 500: a malformed body is their problem, and a 500 storm is what
            # gets an endpoint auto-disabled.
            return jsonify({"error": str(exc)}), 400

        stored = store.append(record)
        return jsonify({"status": "ok", "stored": stored, "duplicate": not stored}), 200

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "mode": config.get("mode"), "identified": len(store)})

    return app


def main():
    ap = argparse.ArgumentParser(description="Unified Dashboards site + RB2B webhook receiver")
    ap.add_argument("--run-name", default="demo", help="names .tmp/visitors/<run-name>/")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--port", type=int)
    ap.add_argument("--host")
    ap.add_argument("--no-resolver", action="store_true", help="serve the site without the mock resolver")
    args = ap.parse_args()

    config = load_config(args.config)
    host = args.host or config.get("site_host", "127.0.0.1")
    port = args.port or config.get("site_port", 5000)

    out = run_dir(args.run_name)
    out.mkdir(parents=True, exist_ok=True)
    store = VisitorStore(out / "identified.jsonl")

    on_pageview = None
    if config.get("mode") != "live" and not args.no_resolver:
        from rb2b_mock_resolver import MockResolver

        webhook_url = f"http://{host}:{port}/rb2b/webhook"
        on_pageview = MockResolver(config, webhook_url=webhook_url).handle

    app = create_app(config, store, on_pageview=on_pageview)
    print(f"MODE={config.get('mode')}", flush=True)
    print(f"SITE_URL=http://{host}:{port}/", flush=True)
    print(f"STORE_PATH={out / 'identified.jsonl'}", flush=True)
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
