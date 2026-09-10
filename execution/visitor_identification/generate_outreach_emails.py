"""
Layer 3 execution tool: draft one personalized outreach email per identified lead.

Written against the client's own playbook. The frameworks in execution/shared/sales_frameworks/
are loaded as raw markdown and injected into the prompt, so editing those files changes
the output with no code change here.

WHICH FRAMEWORKS, AND WHY BOTH:
An identified website visitor sits between the playbook's two DM types, so the generator
uses a blend — see sales_frameworks/15_warm_visitor_hybrid.md. A pure Conversion DM opens
straight on the offer and reads generic to someone who has never spoken to us. A pure
Audience-Building DM spends its energy surfacing pain the visit already proved. The blend
takes the Audience-Building DM's sharp personalized pain hook and fuses it onto the
Conversion DM's offer and CTA. Both files are loaded.

VARIANTS: execution/visitor_identification/email_variants.json defines structural approaches that differ in the
MOVE they make on the pain — the line the email lives or dies on. Active variants rotate
across a batch. Generate the full matrix for approval with generate_email_variants.py.

GROUNDING: every draft is checked against that lead's own enrichment record. A draft naming
a tool the company doesn't run is rejected and regenerated.

Nothing is ever sent. Output is .eml files plus JSON.

CLI usage:
    python execution/visitor_identification/generate_outreach_emails.py --run-name demo --dry-run   # free, no API calls
    python execution/visitor_identification/generate_outreach_emails.py --run-name demo
    python execution/visitor_identification/generate_outreach_emails.py --run-name demo --variants stack_math,blind_spot
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT, SALES_FRAMEWORKS, TECH_STACKS  # noqa: E402  (every area on sys.path)
from spend_gate import add_spend_argument, confirm_spend  # noqa: E402  (lives in shared/)

# Data that belongs to this area lives beside it.
HERE = Path(__file__).resolve().parent

FRAMEWORK_DIR = SALES_FRAMEWORKS  # shared/ — the cold campaign loads the same playbook
STACKS_PATH = TECH_STACKS
VARIANTS_PATH = HERE / "email_variants.json"

# Both DM types load: the hybrid draws on each, and the model needs to see the register of
# both to blend them. 15_warm_visitor_hybrid.md is the file that says how.
DEFAULT_FRAMEWORKS = [
    "00_foundation.md",
    "10_conversion_dm.md",
    "20_audience_building_dm.md",
    "15_warm_visitor_hybrid.md",
    "30_offer.md",
]

DEFAULT_MODEL = "gemini-2.5-flash"

# Who the drafts are from. These land in the .eml From: header and in the sign-off, so they
# are per-deployment rather than per-run: set them in .env once. The fallbacks keep a fresh
# clone runnable and are obviously placeholders, so nobody ships one by accident.
#
# Read at import, not lazily, because build_cold_emails imports SENDER_NAME from this module
# at ITS import time. So .env has to be loaded here too, by absolute path for the same reason
# _get_client does it that way: under launchd there is no shell environment, and a
# cwd-relative load_dotenv() silently finds nothing.
_dotenv_path = ROOT / ".env"
if _dotenv_path.exists():
    from dotenv import load_dotenv

    load_dotenv(_dotenv_path)

SENDER_NAME = os.environ.get("SENDER_NAME", "").strip() or "Your Name"
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "").strip() or "you@example.com"

# The approved call to action. Fixed, not rotated: it's low friction, it's a gift rather
# than a meeting request, and it proves the claim instead of asserting it.
CTA = "offering to send over a sample dashboard built on their actual stack"

WORD_MIN, WORD_MAX = 35, 105   # target is 45-85; this is the hard reject range

BANNED_PHRASES = [
    # Cold-email boilerplate
    "hope this finds you well", "hope you're doing well", "i hope this email finds you",
    "free trial", "sign up", "just checking in", "circling back", "touching base",
    "to whom it may concern", "dear sir", "dear madam", "synergy", "leverage our",
    "revolutionary", "game-changing", "% off", "discount",
    # Converged-batch tell
    "would you be open to",
    # Dead questions. "Is this something you'd be interested in?" gets no reply — the soft
    # open door has to invite a real answer, not a yes/no about interest.
    "are you interested", "would you be interested", "if you're interested",
    "any interest in", "is this something you", "let me know if you'd like",
    # Surveillance. Acknowledging the visit warmly is wanted and fine ("thanks for stopping
    # by"); describing WHICH pages they viewed or how long they stayed is not.
    "i noticed you were looking", "i saw you looking", "saw you checking out",
    "noticed you checking out", "i see you've been on", "you spent time",
    "pricing page", "you viewed", "you clicked on",
]


def exemplar_sentences(variants):
    """Sentences from approved examples that a new draft must not reproduce verbatim.

    The approved CTA is excluded on purpose — it is deliberately fixed, so every email
    ending the same way is the intended behaviour, not copying. Everything else is voice
    to imitate, not text to reuse: asking the model nicely got 4 reused lines in a batch
    of 10, which would be conspicuous at a hundred.
    """
    out = set()
    for variant in variants:
        for example in variant.get("approved_examples", []):
            for sentence in _sentences(example.get("body", "")):
                if "sample dashboard" not in sentence:
                    out.add(sentence)
    return out


def _sentences(text):
    return [
        s.strip().lower()
        for s in re.split(r"[.?!\n]+", text or "")
        if len(s.strip().split()) >= 6
    ]


def all_known_tools():
    """Every tool the detector can emit — the vocabulary a hallucination check needs."""
    data = json.loads(STACKS_PATH.read_text())
    return {entry["tool"] for stack in data["stacks"].values() for entry in stack}


def load_frameworks(names=None):
    chunks = []
    for name in names or DEFAULT_FRAMEWORKS:
        path = FRAMEWORK_DIR / name
        if not path.exists():
            raise FileNotFoundError(f"framework not found: {path}")
        chunks.append(f"===== {name} =====\n\n{path.read_text().strip()}")
    return "\n\n".join(chunks)


def load_variants(only=None):
    variants = json.loads(VARIANTS_PATH.read_text())["variants"]
    if only:
        wanted = [v.strip() for v in only.split(",")] if isinstance(only, str) else list(only)
        by_id = {v["id"]: v for v in variants}
        missing = [w for w in wanted if w not in by_id]
        if missing:
            raise ValueError(
                f"unknown variant(s): {', '.join(missing)} (have: {', '.join(sorted(by_id))})"
            )
        return [by_id[w] for w in wanted]
    return variants


def pick_variant_for(lead, variants):
    """Prefer a variant suited to the lead, else let the caller rotate.

    `blind_spot` is the only one written for someone who already owns a BI tool, so it wins
    when both are true — offering a first dashboard to a Looker shop is the fastest way to
    prove you didn't look.
    """
    by_id = {v["id"]: v for v in variants}
    if lead.get("bi_tool") and "blind_spot" in by_id:
        return by_id["blind_spot"]
    if (lead.get("employee_count") or 999) < 60 and "ultra_short" in by_id:
        return by_id["ultra_short"]
    return None


# ---------------------------------------------------------------------------- prompt


def build_prompt(lead, frameworks, variant):
    attribution = lead.get("attribution") or {}
    stack_lines = "\n".join(
        f"  - {t['tool']} ({t['category']}, detected via {t['source']})"
        for t in lead["tech_stack"]
    ) or "  - nothing detected"

    arrival = "arrived from an ad"
    if attribution.get("is_paid") and attribution.get("angle"):
        arrival = f"clicked a {attribution.get('channel')} ad about \"{attribution['angle']}\""
    elif attribution.get("ad_id") == "organic-search":
        arrival = "found the site through a Google search"
    elif attribution.get("ad_id") == "direct":
        arrival = "came to the site directly"

    segment_directive = {
        "has_bi_disconnected": (
            f"They ALREADY RUN {lead.get('bi_tool')}. Never offer them \"one dashboard\" or "
            f"imply they lack one — they have one. The gap is what it is connected to."
        ),
        "has_bi": f"They already run {lead.get('bi_tool')}. Do not pitch a first dashboard.",
        "many_sources_no_bi": (
            "No BI tool across a lot of sources — the reporting is almost certainly manual, "
            "and you can say so directly."
        ),
        "few_sources_no_bi": "A smaller stack. Keep it proportional — a few tools is not chaos.",
        "no_stack_detected": "No stack detected. Do NOT name or guess at any tool.",
    }.get(lead.get("segment"), "")

    named = lead.get("named_tools") or []
    named_str = ", ".join(named) if named else "(no tools detected — stay generic)"

    # Few-shot exemplars the client approved for this variant. These do the heavy lifting on
    # voice: a framework describes a register, an approved email demonstrates it. The block
    # is emphatic about not reusing the other prospect's facts, and the grounding check
    # catches it anyway if a tool leaks across.
    examples = variant.get("approved_examples") or []
    exemplar_block = ""
    if examples:
        rendered = "\n\n".join(
            f"--- approved example {i} (written for {ex['written_for']}) ---\n"
            f"Subject: {ex['subject']}\n\n{ex['body']}"
            for i, ex in enumerate(examples, start=1)
        )
        exemplar_block = f"""
===== APPROVED EXAMPLES OF THIS VARIANT =====

The client picked these out of a review and wants more like them. They are the strongest
signal you have for voice, rhythm, sentence length and how much to say. Match them closely.

{rendered}

HOW TO USE THEM:
- Match the VOICE and the SHAPE: the plain register, the short lines, the beat order, the
  way the pain lands in one sentence, the way the CTA and the open door sit at the end.
- Do NOT reuse their content. Each was written for a different prospect — the company
  names, the tools, the industry and the specific consequences in them belong to someone
  else. Using any of that for this prospect is a factual error.
- Do NOT copy a sentence verbatim. Write a new email for this prospect that would sit
  naturally alongside these as another one from the same person.
"""

    return f"""You are writing one short outreach email to someone who just visited the
Unified Dashboards website. The playbook below is the client's own and is the authority on
structure and register. Match it exactly.

{frameworks}

===== THE PROSPECT =====

First name (address them by it): {lead['first_name']}
Title: {lead.get('title') or 'unknown — do not guess or reference their title'}
Company: {lead.get('company_name')}
Industry: {lead.get('industry') or 'unknown'}
Headcount: {lead.get('employee_count') or 'unknown'}
Location: {lead.get('city')}, {lead.get('state')}

They {arrival}, then visited the site.

===== THEIR DETECTED TECH STACK =====

{stack_lines}

Reporting sources we would unify ({lead.get('siloed_source_count')}): {', '.join(lead.get('siloed_sources') or []) or 'none'}
Business intelligence tool: {lead.get('bi_tool') or 'none detected'}

{segment_directive}

===== THE VARIANT YOU ARE WRITING: {variant['name']} =====

{variant['pain_move']}
{exemplar_block}

===== HARD REQUIREMENTS =====

1. Open by acknowledging the visit in ONE short line — warm, human, unfussy. This is not
   a cold email and must not read like one. Write this line YOURSELF; do not copy any
   example. Registers that work: a plain thanks for the visit, a light nod to them landing
   on the site, a direct "you came by, so I'll be quick". Vary it — several emails go out
   in the same batch and an identical opening line across them reads as a mail merge.
2. Then the pain hook, executing the variant move above. This is the line the email lives
   or dies on. It must be something you could only write to {lead.get('company_name')} — if
   the sentence would survive being sent to a different company unchanged, rewrite it.
3. Name these exact tools, spelled exactly like this: {named_str}
   Name AT LEAST TWO. Never mention a tool that is not in the detected stack above.
   Naming tools is not enough on its own — say what having them separately COSTS them.
4. Close by {CTA}.
5. Then one short line inviting them to say what actually brought them to the site — what
   they were trying to solve, what question sent them looking, what they hoped to see.
   It must be answerable in a sentence; never "is this something you'd be interested in?",
   which is a dead question. Write this line YOURSELF too, in your own words, and make it
   different from the opening's register. Do not reuse a stock closing across emails.
6. **45 to 85 words total.** Shorter than a normal email on purpose. It should read like
   something a person typed, not something a company sent. Cut any sentence not doing work.
7. Short sentences, line breaks between beats, plain sentence case. No bullet lists and no
   paragraphs of explanation. They visited the site — they know what we do.
8. Do not explain the service or describe the process. No feature lists.
9. Never describe their browsing behaviour — which pages, how long, what they clicked.
   Acknowledging the visit is warm; narrating it is creepy.
10. No discount, no free trial, no gendered pronouns for anyone.
11. Sign off as {SENDER_NAME}. No title, no company footer, no postscript.

Subject line: under 45 characters, lowercase or sentence case, specific to them, no emoji.

Return ONLY a JSON object:
{{"subject": "...", "body": "..."}}
Use \\n for line breaks inside body."""


# ------------------------------------------------------------------------ validation


def validate_draft(draft, lead, known_tools, exemplar_sents=None, banned=None):
    """Return a list of problems. Empty means the draft is safe to hand over.

    `banned` overrides BANNED_PHRASES. The cold campaign passes a superset — everything
    here plus the warm-visit language that is simply false in a cold email — so the two
    areas share one validator instead of forking it.
    """
    problems = []
    subject = (draft.get("subject") or "").strip()
    body = (draft.get("body") or "").strip()

    if not subject:
        problems.append("empty subject")
    elif len(subject) > 60:
        problems.append(f"subject too long ({len(subject)} chars)")
    if not body:
        problems.append("empty body")
        return problems

    words = len(body.split())
    if words < WORD_MIN:
        problems.append(f"body too short ({words} words)")
    elif words > WORD_MAX:
        problems.append(f"body too long ({words} words, target 45-85)")

    if lead["first_name"].lower() not in body.lower():
        problems.append("does not address the lead by first name")

    haystack = f"{subject}\n{body}".lower()

    # THE grounding check: any known tool named must be on this company's stack.
    #
    # Their own tools are masked out first, longest name first. One vendor's product name
    # can contain another's whole name — "Shopify Plus" contains "Shopify" — and a bare
    # word-boundary scan then flags a lead who correctly named Shopify Plus for naming
    # Shopify, a tool they "don't run". Masking makes the check ask the right question:
    # what is left over after removing everything they legitimately run?
    theirs = {t["tool"] for t in lead["tech_stack"]}
    remainder = haystack
    for tool in sorted(theirs, key=len, reverse=True):
        remainder = re.sub(rf"(?<![\w]){re.escape(tool.lower())}(?![\w])", " ", remainder)
    for tool in known_tools:
        if tool in theirs:
            continue
        if re.search(rf"(?<![\w]){re.escape(tool.lower())}(?![\w])", remainder):
            problems.append(f"names a tool they do not run: {tool}")

    # The personalization floor: two of their real tools, from two different categories.
    #
    # This deliberately does NOT require the tools in `named_tools`. That list is a curated
    # pick of the highest-signal tools, one per headline category — a suggestion to the
    # prompt, not a contract. A draft naming three other genuine tools off their stack is
    # exactly as personalized and exactly as grounded, and failing it flags good copy.
    # The category spread is the part that carries the meaning: "Google Ads and Meta Ads"
    # is two ad platforms, not two reporting sources, and that is what this rejects.
    if lead.get("named_tools"):
        mentioned = [t for t in lead["tech_stack"] if t["tool"].lower() in haystack]
        categories = {t.get("category") for t in mentioned}
        if len(mentioned) < 2 or len(categories) < 2:
            problems.append(
                f"names {len(mentioned)} tool(s) across {len(categories)} category/ies; "
                f"need 2+ tools from 2+ categories: {[t['tool'] for t in mentioned]}"
            )

    for phrase in (banned if banned is not None else BANNED_PHRASES):
        if phrase in haystack:
            problems.append(f"banned phrase: '{phrase}'")

    # Imitate the approved voice; don't reproduce its sentences.
    for sentence in _sentences(body):
        if sentence in (exemplar_sents or set()):
            problems.append(f"reuses an approved example verbatim: '{sentence[:60]}...'")

    return problems


# ------------------------------------------------------------------------ generation


def call_gemini(prompt, model=DEFAULT_MODEL, temperature=0.95):
    # Reuse the existing client builder: it loads .env by absolute path, which is what keeps
    # these scripts working under launchd where there is no shell environment.
    from gemini_image_generate import _get_client
    from google.genai import types

    client = _get_client()
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=temperature,
            # We pass no tools; disabling AFC silences the SDK's advisory on every call.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    return json.loads(response.text)


def generate_for_lead(lead, frameworks, known_tools, variant, model=DEFAULT_MODEL,
                      max_attempts=2, exemplar_sents=None):
    """Generate, validate, and retry once with the failures fed back."""
    prompt = build_prompt(lead, frameworks, variant)
    attempts = []

    for _ in range(max_attempts):
        current = prompt
        if attempts:
            current = (
                prompt
                + "\n\n===== YOUR PREVIOUS ATTEMPT WAS REJECTED =====\n"
                + f"Draft: {json.dumps(attempts[-1]['draft'])}\n"
                + "Problems:\n"
                + "\n".join(f"  - {p}" for p in attempts[-1]["problems"])
                + "\nRewrite it, fixing every problem. Same JSON format."
            )
        try:
            draft = call_gemini(current, model=model)
        except Exception as exc:
            attempts.append({"draft": None, "problems": [f"generation error: {exc}"]})
            continue

        problems = validate_draft(draft, lead, known_tools, exemplar_sents)
        attempts.append({"draft": draft, "problems": problems})
        if not problems:
            return draft, [], len(attempts)

    last = attempts[-1]
    return last["draft"], last["problems"], len(attempts)


def to_eml(lead, draft, variant_id=None):
    msg = EmailMessage()
    msg["To"] = f"{lead.get('full_name')} <{lead.get('business_email')}>"
    msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
    msg["Subject"] = draft.get("subject", "")
    msg["X-UD-Lead-Score"] = str(lead.get("lead_score"))
    msg["X-UD-Segment"] = str(lead.get("segment"))
    msg["X-UD-Variant"] = str(variant_id)
    msg["X-UD-Ad-Id"] = str((lead.get("attribution") or {}).get("ad_id"))
    msg["X-UD-Named-Tools"] = ", ".join(lead.get("named_tools") or [])
    msg["X-UD-Draft-Only"] = "generated by execution/visitor_identification/generate_outreach_emails.py, never sent"
    msg.set_content(draft.get("body", ""))
    return msg


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "lead").lower()).strip("-")


def main():
    ap = argparse.ArgumentParser(description="Draft outreach emails for identified leads")
    ap.add_argument("--run-name", default="demo")
    ap.add_argument("--in", dest="infile", help="defaults to .tmp/visitors/<run>/enriched.json")
    ap.add_argument("--out", dest="outdir", help="defaults to .tmp/visitors/<run>/emails")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--only", help="comma-separated company domains or first names")
    ap.add_argument("--variants", help="comma-separated variant ids; defaults to config, then all")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true", help="write prompts, spend nothing")
    add_spend_argument(ap)
    args = ap.parse_args()

    from rb2b_site import load_config, run_dir

    base = run_dir(args.run_name)
    infile = Path(args.infile) if args.infile else base / "enriched.json"
    outdir = Path(args.outdir) if args.outdir else base / "emails"

    if not infile.exists():
        print(f"ERROR: no enriched leads at {infile}", flush=True)
        print("DRAFTED=0", flush=True)
        return 1

    leads = json.loads(infile.read_text())
    if args.only:
        wanted = {w.strip().lower() for w in args.only.split(",")}
        leads = [
            l for l in leads
            if (l.get("company_domain") or "").lower() in wanted
            or (l.get("first_name") or "").lower() in wanted
        ]
    if args.limit:
        leads = leads[: args.limit]

    config = load_config()
    selection = args.variants or config.get("active_variants") or None
    try:
        variants = load_variants(selection)
    except ValueError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1

    frameworks = load_frameworks()
    known_tools = all_known_tools()
    exemplar_sents = exemplar_sentences(variants)
    outdir.mkdir(parents=True, exist_ok=True)

    # Assign a variant per lead: a suited one where the lead calls for it, else rotate.
    assignments, rotation = [], 0
    for lead in leads:
        variant = pick_variant_for(lead, variants)
        if variant is None:
            variant = variants[rotation % len(variants)]
            rotation += 1
        assignments.append((lead, variant))

    if args.dry_run:
        prompt_dir = outdir / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        for lead, variant in assignments:
            (prompt_dir / f"{slugify(lead.get('company_name'))}.txt").write_text(
                build_prompt(lead, frameworks, variant)
            )
        print(f"  [dry-run] wrote {len(assignments)} prompts to {prompt_dir}", flush=True)
        print(f"  [dry-run] frameworks: {', '.join(DEFAULT_FRAMEWORKS)}", flush=True)
        print(f"  [dry-run] variants: {', '.join(v['id'] for v in variants)}", flush=True)
        print("DRAFTED=0", flush=True)
        print(f"PROMPTS_PATH={prompt_dir}", flush=True)
        return 0

    # Immediately before the first paid call, and after --dry-run has already returned:
    # parsing, variant assignment and the whole dry-run path are free and must stay
    # runnable without approval.
    if not confirm_spend(calls=len(assignments), model=args.model,
                         label="draft visitor outreach emails", assume_yes=args.yes_spend):
        print("DRAFTED=0", flush=True)
        return 1

    results, failed = [], []
    for lead, variant in assignments:
        name = lead.get("full_name")
        draft, problems, attempts = generate_for_lead(
            lead, frameworks, known_tools, variant, model=args.model,
            exemplar_sents=exemplar_sents,
        )
        if draft is None:
            print(f"  [draft] FAILED {name}: {problems}", flush=True)
            failed.append(name)
            continue

        slug = slugify(lead.get("company_name"))
        eml_path = outdir / f"{slug}.eml"
        eml_path.write_bytes(bytes(to_eml(lead, draft, variant["id"])))
        words = len((draft.get("body") or "").split())

        results.append({
            "first_name": lead.get("first_name"),
            "full_name": name,
            "title": lead.get("title"),
            "company_name": lead.get("company_name"),
            "company_domain": lead.get("company_domain"),
            "to": lead.get("business_email"),
            "linkedin_url": lead.get("linkedin_url"),
            "subject": draft.get("subject"),
            "body": draft.get("body"),
            "word_count": words,
            "variant_id": variant["id"],
            "variant_name": variant["name"],
            "named_tools": lead.get("named_tools"),
            "segment": lead.get("segment"),
            "lead_score": lead.get("lead_score"),
            "attribution": lead.get("attribution"),
            "attempts": attempts,
            "needs_review": bool(problems),
            "problems": problems,
            "eml_path": str(eml_path),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
        })

        flag = f"  NEEDS REVIEW: {problems}" if problems else ""
        retry = "" if attempts == 1 else f" ({attempts} attempts)"
        print(
            f"  [draft] {name:<18} {variant['id']:<15} {words:>3}w "
            f"{draft.get('subject', '')[:42]!r}{retry}{flag}",
            flush=True,
        )

    # A run that produced nothing must not write. An empty drafts.json over a good one
    # destroys a batch that cost real money, and exiting 0 lets the orchestrator carry the
    # emptiness forward into the CSV export and the dashboard as though it were a real
    # result. Same lesson the cold pipeline already learned in build_cold_emails.
    if not results:
        print("\nERROR: no drafts were produced, so nothing was written.", flush=True)
        print("  Check the errors above before re-running.", flush=True)
        print("DRAFTED=0", flush=True)
        if failed:
            print(f"FAILED_LEADS={', '.join(failed)}", flush=True)
        return 1

    drafts_path = outdir / "drafts.json"
    drafts_path.write_text(json.dumps(results, indent=2))

    clean = sum(1 for r in results if not r["needs_review"])
    print(f"DRAFTED={len(results)}", flush=True)
    print(f"CLEAN={clean}", flush=True)
    print(f"NEEDS_REVIEW={len(results) - clean}", flush=True)
    print(f"DRAFTS_PATH={drafts_path}", flush=True)
    if failed:
        print(f"FAILED_LEADS={', '.join(failed)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
