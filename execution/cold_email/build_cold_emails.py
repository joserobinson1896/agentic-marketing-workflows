"""
Layer 3 execution tool: write one personalized cold email per lead in a scraped CSV, and
emit the same CSV back with the copy added as new columns.

The output columns are the point. `personalized_email` is what gets pushed to Instantly as
a per-lead custom variable, so the campaign body is literally `{{personalized_email}}` and
all 100 emails are different. See directives/cold_email_campaign.md.

WHERE THE COPY COMES FROM:
The same approved script as the warm visitor pipeline. execution/shared/sales_frameworks/
is the client's playbook and the authority on register; 16_cold_lead.md is the delta that
replaces the two beats assuming a website visit (the warm open and "what brought you to the
site"). Everything that got approved — the stack-math pain hook, the one-line solution, the
sample-dashboard CTA — is unchanged. Editing those files changes the output with no code
change here.

REUSE, NOT A FORK: segmentation (pick_named_tools, score_lead) comes from enrich_visitors
and validation (validate_draft, exemplar_sentences) from generate_outreach_emails. This
file adds exactly three things: a CSV tech-stack parser, a cold prompt, and the extra
banned phrases that catch warm language leaking into a cold send.

CLI usage:
    python execution/cold_email/build_cold_emails.py --dry-run              # free, writes prompts
    python execution/cold_email/build_cold_emails.py --limit 5              # cheap smoke test
    python execution/cold_email/build_cold_emails.py                        # the full list
"""

import argparse
import csv
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT, SALES_FRAMEWORKS  # noqa: E402  (also puts every area on sys.path)

from enrich_visitors import pick_named_tools, score_lead  # noqa: E402
from generate_outreach_emails import (  # noqa: E402
    BANNED_PHRASES,
    CTA,
    SENDER_NAME,
    WORD_MAX,
    WORD_MIN,
    all_known_tools,
    call_gemini,
    exemplar_sentences,
    slugify,
    validate_draft,
)

HERE = Path(__file__).resolve().parent

VARIANTS_PATH = HERE / "cold_email_variants.json"
# The reference list, kept beside the scripts rather than in disposable .tmp/ for the same
# reason the image pipeline vendors its reference photos: a fresh clone has to be able to
# run --dry-run, and every claim this area's directive makes about variant distribution is
# measured against THIS list. Fully fictional: invented companies, reserved 555-01xx phones.
DEFAULT_LEADS = Path(__file__).resolve().parent / "sample_leads_100.csv"
from spend_gate import add_spend_argument, confirm_spend  # noqa: E402  (lives in shared/)

DEFAULT_OUTDIR = ROOT / ".tmp" / "cold_email"

DEFAULT_MODEL = "gemini-2.5-flash"

# Workers write progress lines from several threads; without this they interleave into
# unreadable half-lines. It also guards the completed counter.
PRINT_LOCK = threading.Lock()

# One draft takes about a minute, almost all of it waiting on the model, so the work is
# purely IO-bound and threads are the right tool. 8 keeps a 100-lead batch near 12 minutes
# without hammering the API hard enough to spend the run in backoff.
DEFAULT_WORKERS = 8

# 16_cold_lead.md is written as a delta on 15_warm_visitor_hybrid.md, so both load: the
# hybrid carries the structure that was approved, the cold file says which two beats are
# replaced and comes after it so it has the last word.
COLD_FRAMEWORKS = [
    "00_foundation.md",
    "10_conversion_dm.md",
    "20_audience_building_dm.md",
    "15_warm_visitor_hybrid.md",
    "16_cold_lead.md",
    "30_offer.md",
]

# Warm language is a factual error in a cold email — these people have never been near the
# site. The warm framework loads into the prompt (16_cold_lead.md is a delta on it) and it
# is full of "thanks for stopping by", so this is the guard that stops the model copying it.
COLD_BANNED_PHRASES = BANNED_PHRASES + [
    # Claims of a visit or prior contact that never happened
    "stopping by", "stopped by", "stop by", "thanks for visiting", "for visiting",
    "your visit", "you visited", "came by", "checking out our", "on our site",
    "on our website", "glad you", "since you were", "you were looking at",
    "your interest in", "following up on", "as promised", "great connecting",
    "nice to e-meet", "we spoke", "our conversation",
    # Announcing the cold email instead of starting at the observation
    "reaching out", "reached out", "quick question for you", "hope you don't mind",
    "sorry to bother", "cold email", "i'll be brief", "let me introduce",
    # Explaining the method, the tell that turns an observation into surveillance
    "i noticed", "i was looking at", "i came across", "i stumbled",
    "while researching", "i was researching", "doing some research",
    "i see that you", "looks like you're using", "according to your website",
    # A SECOND ASK. The warm email closed with a soft open door after the CTA; on a cold
    # lead that is a competing call to action, and two asks split the reply between them
    # until neither is the obvious thing to do. The structural check below is the real
    # guard; these catch the phrasings that survive inside the CTA's own paragraph.
    "or tell me", "or just tell me", "or let me know", "or share", "or reply",
    "or if you", "or drop me", "or send me", "and i'll tell you if",
    # Naming their tools is fine; announcing that we have an inventory of them is not.
    # "Your tech stack includes X, Y and Z" is the same intrusiveness as counting their
    # systems, just spelled differently: it frames the email as a report about them.
    # (The CTA's "built on your actual stack" is untouched by these.)
    "tech stack", "your stack includes", "stack includes", "your toolset",
    "your software stack", "your current stack is", "we detected", "our data shows",
]

# Reporting sources Unified Dashboards would unify. Extends the visitor pipeline's list
# with the three categories this lead list carries that the mock stacks never did —
# Accounting, ERP and Support all hold numbers that belong in a revenue dashboard.
DATA_SOURCE_CATEGORIES = {
    "CRM", "Payments", "Email Marketing", "Web Analytics", "Advertising", "CDP",
    "Product Analytics", "Ecommerce", "Data Warehouse", "BI", "Subscription Billing",
    "Sales Engagement", "Accounting", "ERP", "Support",
}

BI_TOOLS = {"Looker", "Tableau", "Power BI", "Mode", "Metabase", "Sigma"}

# "Tool Name (Category)" — the shape the scraper writes into the tech_stack column.
STACK_ENTRY = re.compile(r"^(?P<tool>.+?)\s*\((?P<category>[^()]+)\)$")


# ------------------------------------------------------------------------- lead loading


def parse_stack(raw):
    """'Stripe (Payments); Mode (BI)' -> the normalized enrichment contract.

    Same four keys every enrichment provider returns, so everything downstream —
    pick_named_tools, the prompt, validate_draft's hallucination check — works unchanged.
    `confidence` is 1.0 because a scraped list is an assertion, not a detection: there is
    no probability to report, and inventing one would be a lie the ranking then trusts.
    """
    out = []
    for chunk in (raw or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = STACK_ENTRY.match(chunk)
        if not match:
            # Keep the tool, lose only the category. A stack entry we can't parse is still
            # a tool they run, and dropping it would let the model "hallucinate" a real one.
            out.append({"tool": chunk, "category": "Other", "confidence": 1.0, "source": "lead_list"})
            continue
        out.append({
            "tool": match.group("tool").strip(),
            "category": match.group("category").strip(),
            "confidence": 1.0,
            "source": "lead_list",
        })
    return out


def to_int(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def build_lead(row):
    """One CSV row -> the lead shape the prompt and the validator already understand."""
    stack = parse_stack(row.get("tech_stack"))
    siloed = [t["tool"] for t in stack if t["category"] in DATA_SOURCE_CATEGORIES]
    bi_tool = next((t["tool"] for t in stack if t["tool"] in BI_TOOLS), None)
    employees = to_int(row.get("employee_count"))

    if not stack:
        segment = "no_stack_detected"
    elif bi_tool and len(siloed) >= 4:
        segment = "has_bi_disconnected"
    elif bi_tool:
        segment = "has_bi"
    elif len(siloed) >= 4:
        segment = "many_sources_no_bi"
    else:
        segment = "few_sources_no_bi"

    record = {"employee_count": employees, "title": row.get("job_title")}

    return {
        "first_name": (row.get("first_name") or "").strip(),
        "full_name": (row.get("full_name") or "").strip(),
        "last_name": (row.get("last_name") or "").strip(),
        "email": (row.get("email") or "").strip(),
        "title": (row.get("job_title") or "").strip(),
        "company_name": (row.get("company_name") or "").strip(),
        "company_domain": (row.get("company_domain") or "").strip(),
        "industry": (row.get("company_industry") or "").strip(),
        "employee_count": employees,
        "city": (row.get("person_city") or "").strip(),
        "state": (row.get("person_state") or "").strip(),
        "linkedin_url": (row.get("linkedin_url") or "").strip(),
        "tech_stack": stack,
        "siloed_sources": siloed,
        "siloed_source_count": len(siloed),
        "bi_tool": bi_tool,
        "named_tools": pick_named_tools(stack, set(siloed)),
        "segment": segment,
        # No attribution for a cold lead — nobody clicked anything. score_lead reads
        # is_paid off this dict, so an empty one is the honest input, not a missing one.
        "lead_score": score_lead(record, len(siloed), bi_tool, {}),
    }


def load_leads(path):
    # utf-8-sig: the scraper writes a BOM, and without this the first column name comes
    # back as "﻿full_name" and every full_name lookup silently returns empty.
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader), reader.fieldnames


# ------------------------------------------------------------------------------ prompt


def load_frameworks(names=None):
    chunks = []
    for name in names or COLD_FRAMEWORKS:
        path = SALES_FRAMEWORKS / name
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


def eligible_variants(lead, variants):
    """The variants that are not structurally false for this lead.

    A variant whose whole move depends on the lead owning a BI tool cannot rotate onto a
    lead who doesn't, and vice versa. Routing alone is not enough: it only handles the
    lead a variant is *best* for, and leaves the rotation free to hand blind_spot ("you
    already have the dashboard, the gap is what feeds it") to a company with no BI tool —
    asserting something false while the segment directive in the same prompt says the
    opposite. Gating the rotation pool is what actually prevents that.
    """
    has_bi = bool(lead.get("bi_tool"))
    pool = [v for v in variants if v.get("requires_bi") in (None, has_bi)]
    return pool or list(variants)


def pick_variant_for(lead, variants):
    """Same routing the warm pipeline uses, and for the same reasons."""
    by_id = {v["id"]: v for v in variants}
    if lead.get("bi_tool") and "blind_spot" in by_id:
        return by_id["blind_spot"]
    if (lead.get("employee_count") or 999) < 60 and "ultra_short" in by_id:
        return by_id["ultra_short"]
    return None


def build_cold_prompt(lead, frameworks, variant):
    stack_lines = "\n".join(
        f"  - {t['tool']} ({t['category']})" for t in lead["tech_stack"]
    ) or "  - nothing detected"

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

    examples = variant.get("approved_examples") or []
    exemplar_block = ""
    if examples:
        rendered = "\n\n".join(
            f"--- approved example {i} (written for {ex['written_for']}) ---\n"
            f"Subject: {ex['subject']}\n\n{ex['body']}"
            for i, ex in enumerate(examples, start=1)
        )
        exemplar_block = f"""
===== APPROVED EXAMPLES OF THIS VARIANT (COLD) =====

These are the cold form of the client's approved copy. They are the strongest signal you
have for voice, rhythm, sentence length and how much to say. Match them closely.

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

    return f"""You are writing one short COLD outreach email for Unified Dashboards. The
playbook below is the client's own and is the authority on structure and register.

READ THIS FIRST — IT OVERRIDES ANYTHING IN THE PLAYBOOK THAT CONTRADICTS IT:
This person has NEVER visited our website. They have never heard of us, never clicked an
ad, never filled in a form and never spoken to anyone here. 15_warm_visitor_hybrid.md was
written for people who did visit, and its examples open by thanking someone for a visit.
**That opening is a lie here and is forbidden.** 16_cold_lead.md is the file that governs
this email: it keeps the hybrid's pain hook, solution line and CTA exactly as approved, it
replaces the warm open with a flat observation, and it DELETES the warm version's closing
"what brought you to the site" line rather than replacing it. A cold email ends on its one
call to action.

{frameworks}

===== THE PROSPECT =====

First name (address them by it): {lead['first_name']}
Title: {lead.get('title') or 'unknown — do not guess or reference their title'}
Company: {lead.get('company_name')}
Industry: {lead.get('industry') or 'unknown'}
Headcount: {lead.get('employee_count') or 'unknown'}
Location: {lead.get('city')}, {lead.get('state')}

===== THEIR TECH STACK =====

{stack_lines}

Reporting sources we would unify ({lead.get('siloed_source_count')}): {', '.join(lead.get('siloed_sources') or []) or 'none'}
Business intelligence tool: {lead.get('bi_tool') or 'none detected'}

{segment_directive}

===== THE VARIANT YOU ARE WRITING: {variant['name']} =====

{variant['pain_move']}
{exemplar_block}

===== HARD REQUIREMENTS =====

1. Open on a flat, specific observation about THEM — their stack, their company, their
   situation. No greeting-plus-throat-clearing, no thanks, no reference to a visit, a
   click, a form, a download or any prior contact, because none happened. Do NOT explain
   how you know what you know: no "I noticed", no "I was looking at your site", no "I came
   across your company". State the fact and move on. Write this line YOURSELF; do not copy
   any example. Vary it — a hundred of these send in one batch and an identical opening
   line across them reads as a mail merge.
2. Then the pain hook, executing the variant move above. This is the line the email lives
   or dies on. It must be something you could only write to {lead.get('company_name')} — if
   the sentence would survive being sent to a different company unchanged, rewrite it.
   Because the open is now an observation too, beats 1 and 2 sit close together. Let them
   run into each other rather than padding them apart.
3. Name these exact tools, spelled exactly like this: {named_str}
   Name AT LEAST TWO. Never mention a tool that is not in the stack above.
   Naming tools is not enough on its own, so say what having them separately COSTS them.
   List them ONCE. Repeating the same set of tools in the next sentence reads as padding.
   Weave them into a sentence about their situation. Never announce them as an inventory
   ("your tech stack includes X, Y and Z"), which frames the email as a report about them
   and reads exactly as intrusive as counting their systems.
4. **NEVER say how many tools, systems, sources or platforms they have.** Not "nine
   reporting systems", not "three different answers", not any number attached to their
   stack. The list above is only what is publicly visible; they run things we cannot see,
   so a count is both intrusive and probably wrong, and being confidently wrong in line two
   ends the email. Say "each hold a piece", "no single place", "depends which one you open".
   Vague quantifiers only. ("one view", "one number" as the SOLUTION is fine.)
5. Close by {CTA}. **This is the last thing the email says.** After it comes your sign-off
   and NOTHING else.
6. **ONE call to action, never two.** Do not add a softer second ask after the CTA. No "or
   tell me how you handle it today", no "or let me know", no second question of any kind,
   in its own paragraph or tacked onto the end of the CTA sentence. Two asks split the
   reply between them until neither is the obvious thing to do, and the reader does nothing.
7. **NEVER use an em dash or an en dash**, in the subject or the body. Use a comma, a full
   stop, or start a new sentence.
8. **40 to 75 words total.** Shorter than a normal email on purpose. It should read like
   something a person typed, not something a company sent. Cut any sentence not doing work.
9. Short sentences and plain sentence case. Separate EVERY beat with a BLANK LINE: the
   open, the pain hook, the solution, the CTA and the sign-off are each their own
   paragraph. No bullet lists and no paragraphs of explanation.
10. Do not explain the service or describe the process. No feature lists.
11. No discount, no free trial, no gendered pronouns for anyone.
12. Sign off as {SENDER_NAME}. No title, no company footer, no postscript, no unsubscribe
    line, since the sending platform adds the opt-out header.

Subject line: under 45 characters, lowercase or sentence case, specific to them, no emoji,
no em dash, and no count of their systems. It must not promise a reply to something they
never sent.

Return ONLY a JSON object:
{{"subject": "...", "body": "..."}}
Use \\n for line breaks inside body."""


# -------------------------------------------------------------------------- generation


# The framework asks for a line break between beats. The model obeys most of the time and
# then emits a wall of text: one draft in the first smoke batch came back as 2 paragraphs
# where the rest of the batch ran 5-6. Beside its neighbours in an inbox that reads as a
# different sender, so it is a reject rather than a note.
MIN_PARAGRAPHS = 4

# The paragraph that carries the approved ask is found by this, so everything after it can
# be required to be nothing but a sign-off.
CTA_MARKER = "sample dashboard"

# A sign-off is short and asks nothing. Anything longer sitting after the CTA is a second
# call to action wearing a different hat.
SIGNOFF_MAX_WORDS = 3

# A count of someone's internal systems is banned for two reasons, and the second is the one
# that matters: the detected stack is only what is publicly visible, so they almost certainly
# run tools we cannot see, and "you operate nine reporting systems" is therefore probably
# false. Being confidently wrong in line two ends the email. "one" is deliberately absent
# from the alternation, because "one view" and "one number" are the offer.
STACK_COUNT = re.compile(
    r"\b(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
    r"(?:\w+\s+){0,2}"
    r"(tools?|systems?|sources?|platforms?|dashboards?|answers?|views?|places?|"
    r"stacks?|apps?|integrations?|silos?)\b",
    re.IGNORECASE,
)

DASH_PRESENT = re.compile(r"[\u2014\u2013]")


def check_cold_rules(draft, lead):
    """The rules that are specific to cold copy. Same contract as validate_draft.

    These live here rather than in the shared validator because none of them is about
    whether the email is TRUE, which is what the shared checks police. They are about what
    a cold message may do: one ask, no dashes, and no assertion about the size of someone's
    stack that we are not in a position to make.
    """
    problems = []
    body = (draft.get("body") or "").strip()
    subject = (draft.get("subject") or "").strip()
    if not body:
        return problems

    text = f"{subject}\n{body}"

    if DASH_PRESENT.search(text):
        problems.append("uses an em or en dash; sales copy takes a comma or a full stop")

    # Mask their own tools out before looking for counts, so a product name carrying a
    # numeral ("Google Analytics 4") cannot be read as a claim about how many things they
    # run. Longest first, for the same reason the grounding check does it that way.
    remainder = text
    for tool in sorted({t["tool"] for t in lead.get("tech_stack") or []}, key=len, reverse=True):
        remainder = re.sub(rf"(?<![\w]){re.escape(tool)}(?![\w])", " ", remainder,
                           flags=re.IGNORECASE)
    counted = STACK_COUNT.search(remainder)
    if counted:
        problems.append(
            f"states how many systems they have ({counted.group(0).strip()!r}); the "
            f"detected stack is partial, so any count is intrusive and probably wrong"
        )

    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    if len(paragraphs) < MIN_PARAGRAPHS:
        problems.append(
            f"only {len(paragraphs)} paragraph(s); the beats must be separated by blank "
            f"lines (need {MIN_PARAGRAPHS}+)"
        )

    # One ask. The CTA is the last thing said.
    cta_index = next(
        (i for i, para in enumerate(paragraphs) if CTA_MARKER in para.lower()), None
    )
    if cta_index is None:
        problems.append(f"no call to action; the email must offer a {CTA_MARKER}")
    else:
        for para in paragraphs[cta_index + 1:]:
            if "?" in para or len(para.split()) > SIGNOFF_MAX_WORDS:
                problems.append(
                    f"a second ask after the CTA ({para[:48]!r}); a cold message gets one "
                    f"call to action and the sign-off is all that may follow it"
                )
                break

    return problems


# Transient failures worth waiting out rather than counting as a rejected draft. Running
# the batch in parallel makes a rate limit likely where sequential never saw one, and
# generate_for_lead only allows two attempts TOTAL: without this, two 429s in a row would
# be recorded as a lead that could not be written, which is a silent hole in the batch.
TRANSIENT_MARKERS = ("429", "rate limit", "resource_exhausted", "quota",
                     "503", "unavailable", "500", "internal error", "deadline")

# A 429 is NOT automatically transient. A spending cap or a dead key returns the same status
# as a rate limit and will still be there in thirty seconds, so retrying it wastes the
# backoff on every lead: a capped project burned 40 seconds per draft before failing, which
# across 100 leads is over an hour of sleeping to arrive at the same answer. These are
# checked FIRST and abort the whole batch rather than being retried.
# Both spellings of each: Google returns the status as PERMISSION_DENIED (underscore) and
# the prose as "permission denied" (space), and matching is done on the lowercased message.
FATAL_MARKERS = ("spending cap", "spend cap", "exceeded its monthly", "billing",
                 "api key not valid", "invalid api key",
                 "permission denied", "permission_denied",
                 "api_key_invalid", "consumer_suspended", "unauthenticated")
RATE_LIMIT_RETRIES = 5


class FatalGenerationError(RuntimeError):
    """The API will keep saying no. Stop the batch instead of working through it."""


def call_gemini_with_backoff(prompt, model=DEFAULT_MODEL, retries=RATE_LIMIT_RETRIES):
    """call_gemini, but a rate limit waits instead of counting as a failed attempt.

    Backoff is exponential with jitter. The jitter matters more than usual here: every
    worker that hits the same limit at the same moment would otherwise retry in lockstep
    and hit it again together.
    """
    for attempt in range(retries):
        try:
            return call_gemini(prompt, model=model)
        except Exception as exc:
            message = str(exc).lower()
            if any(marker in message for marker in FATAL_MARKERS):
                raise FatalGenerationError(str(exc)) from exc
            transient = any(marker in message for marker in TRANSIENT_MARKERS)
            if not transient or attempt == retries - 1:
                raise
            wait = min(2 ** attempt, 30) + random.uniform(0, 1.5)
            with PRINT_LOCK:
                print(f"  [cold] rate limited, waiting {wait:.1f}s "
                      f"({attempt + 1}/{retries})", flush=True)
            time.sleep(wait)


def generate_for_lead(lead, frameworks, known_tools, variant, model=DEFAULT_MODEL,
                      max_attempts=2, exemplar_sents=None):
    """Generate, validate against this lead's own record, retry once with the failures."""
    prompt = build_cold_prompt(lead, frameworks, variant)
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
            draft = call_gemini_with_backoff(current, model=model)
        except FatalGenerationError:
            raise  # not a bad draft; the API is refusing everything
        except Exception as exc:
            attempts.append({"draft": None, "problems": [f"generation error: {exc}"]})
            continue

        # Normalize first, then judge what will actually be sent. Validating the raw draft
        # would reject dashes the normalizer was about to remove, spending a regeneration
        # on a problem already solved.
        draft = {
            "subject": normalize_typography(draft.get("subject")),
            "body": normalize_typography(draft.get("body")),
        }
        problems = validate_draft(
            draft, lead, known_tools, exemplar_sents, banned=COLD_BANNED_PHRASES
        ) + check_cold_rules(draft, lead)
        attempts.append({"draft": draft, "problems": problems})
        if not problems:
            return draft, [], len(attempts)

    last = attempts[-1]
    return last["draft"], last["problems"], len(attempts)


# Typography the model gets wrong at random, fixed deterministically rather than asked for
# in a prompt instruction it follows only sometimes. Smart quotes turned up in two of seven
# drafts in the first smoke batch, which leaves a batch typographically mixed.
SMART_QUOTES = {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u2026": "..."}

# Em and en dashes are banned outright in sales copy. A spaced dash is almost always doing
# a comma's job, so swapping it is safe and saves a regeneration; check_cold_rules still
# rejects any that survive, which keeps the rule true rather than merely attempted.
DASHES = re.compile(r"\s*[\u2014\u2013]\s*")


def normalize_typography(text):
    text = text or ""
    for smart, plain in SMART_QUOTES.items():
        text = text.replace(smart, plain)
    text = DASHES.sub(", ", text)
    # Tidy what the swap can leave behind: doubled commas, a space before one, or a comma
    # that has landed against the punctuation that ended the sentence anyway.
    text = re.sub(r",\s*,+", ",", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",\s*([.?!])", r"\1", text)
    return text.strip()


def to_html(body):
    """Plain body -> the HTML Instantly's editor expects, blank lines kept as blank lines."""
    from html import escape
    paragraphs = [p.strip() for p in (body or "").split("\n\n") if p.strip()]
    # quote=False: this lands in body text, not an attribute, so escaping ' and " only
    # turns every apostrophe in the copy into &#x27; for no benefit. & < > still escape.
    return "<br><br>".join(escape(p, quote=False).replace("\n", "<br>") for p in paragraphs)


# ------------------------------------------------------------------------------- output

# Appended to every row of the source CSV. `personalized_email` is the one that matters:
# it becomes the Instantly custom variable the campaign body is built from.
NEW_COLUMNS = [
    "email_subject",
    "personalized_email",
    "personalized_email_html",
    "email_variant",
    "email_word_count",
    "email_needs_review",
]


def revalidate(outdir, leads_path, known_tools, exemplar_sents):
    """Re-apply the current checks to an already-generated batch. Spends nothing.

    Tightening a rule shouldn't mean paying to regenerate a hundred emails to find out
    which ones it catches, and loosening one that was flagging good copy shouldn't leave
    the flag stuck on. This re-runs validation over the stored drafts and rewrites the
    flags in both drafts.json and the CSV.
    """
    drafts_path = outdir / "drafts.json"
    csv_path = outdir / f"{leads_path.stem}_personalized.csv"
    if not drafts_path.exists():
        print(f"ERROR: nothing to revalidate at {drafts_path}", flush=True)
        return 1

    rows, fieldnames = load_leads(leads_path)
    leads_by_email = {}
    for row in rows:
        lead = build_lead(row)
        leads_by_email[lead["email"].lower()] = lead

    results = json.loads(drafts_path.read_text())
    changed = 0
    for result in results:
        lead = leads_by_email.get((result.get("email") or "").lower())
        if lead is None:
            continue
        draft = {"subject": result["subject"], "body": result["body"]}
        draft = {"subject": normalize_typography(draft["subject"]),
                 "body": normalize_typography(draft["body"])}
        result["subject"], result["body"] = draft["subject"], draft["body"]
        problems = validate_draft(
            draft, lead, known_tools, exemplar_sents, banned=COLD_BANNED_PHRASES
        ) + check_cold_rules(draft, lead)
        was = result["needs_review"]
        result["problems"] = problems
        result["needs_review"] = bool(problems)
        if was != result["needs_review"]:
            changed += 1
            verb = "now FLAGGED" if problems else "now clean"
            print(f"  [revalidate] {result['full_name']:<20} {verb}: {problems}", flush=True)

    drafts_path.write_text(json.dumps(results, indent=2))

    if csv_path.exists():
        flags = {r["email"].lower(): r for r in results}
        with open(csv_path, newline="", encoding="utf-8-sig") as handle:
            csv_rows = list(csv.DictReader(handle))
        for row in csv_rows:
            result = flags.get((row.get("email") or "").strip().lower())
            if result:
                row["email_needs_review"] = "yes" if result["needs_review"] else "no"
            # Re-render the HTML from the plain body: a fix to to_html should reach an
            # existing batch without paying to regenerate the copy it renders.
            row["personalized_email_html"] = to_html(row.get("personalized_email"))
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fieldnames) + NEW_COLUMNS)
            writer.writeheader()
            writer.writerows(csv_rows)

    clean = sum(1 for r in results if not r["needs_review"])
    print(f"REVALIDATED={len(results)}", flush=True)
    print(f"CHANGED={changed}", flush=True)
    print(f"CLEAN={clean}", flush=True)
    print(f"NEEDS_REVIEW={len(results) - clean}", flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Write a personalized cold email per lead")
    ap.add_argument("--leads", default=str(DEFAULT_LEADS), help="source lead CSV")
    ap.add_argument("--run-name", default="mach100")
    ap.add_argument("--out", dest="outdir", help="defaults to .tmp/cold_email/<run-name>")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--variants", help="comma-separated variant ids; defaults to all")
    ap.add_argument("--limit", type=int, help="only the first N leads (smoke test)")
    ap.add_argument("--only", help="comma-separated emails, first names or company domains; "
                                   "regenerate specific leads without paying for the batch")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"parallel generation workers (default {DEFAULT_WORKERS}); "
                         f"lower it if the API starts rate limiting")
    ap.add_argument("--dry-run", action="store_true", help="write prompts, spend nothing")
    ap.add_argument("--revalidate", action="store_true",
                    help="re-check an existing batch against the current rules; spends nothing")
    add_spend_argument(ap)
    args = ap.parse_args()

    leads_path = Path(args.leads)
    if not leads_path.exists():
        print(f"ERROR: no lead list at {leads_path}", flush=True)
        return 1

    outdir = Path(args.outdir) if args.outdir else DEFAULT_OUTDIR / args.run_name
    outdir.mkdir(parents=True, exist_ok=True)

    rows, fieldnames = load_leads(leads_path)
    if args.only:
        wanted = {w.strip().lower() for w in args.only.split(",") if w.strip()}
        rows = [
            row for row in rows
            if (row.get("email") or "").strip().lower() in wanted
            or (row.get("first_name") or "").strip().lower() in wanted
            or (row.get("company_domain") or "").strip().lower() in wanted
        ]
        if not rows:
            print(f"ERROR: --only matched no leads in {leads_path}", flush=True)
            return 1
    if args.limit:
        rows = rows[: args.limit]

    try:
        variants = load_variants(args.variants)
    except ValueError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1

    exemplar_sents = exemplar_sentences(variants)

    if args.revalidate:
        known = all_known_tools() | {
            t["tool"] for row in rows for t in parse_stack(row.get("tech_stack"))
        }
        return revalidate(outdir, leads_path, known, exemplar_sents)

    frameworks = load_frameworks()

    # The hallucination vocabulary is the union of every tool in THIS list and every tool
    # the visitor pipeline knows. A superset is strictly safer: a tool missing from the
    # vocabulary is a tool the model can invent without being caught.
    known_tools = all_known_tools() | {
        t["tool"] for row in rows for t in parse_stack(row.get("tech_stack"))
    }

    leads = [build_lead(row) for row in rows]

    # Rotation counts globally, not per pool, so consecutive leads still get different
    # variants even though each one draws from its own eligible set.
    # 20 of the 100 companies on this list have two contacts, and colleagues compare notes.
    # Two people at one company receiving the same variant produces near-identical emails on
    # the same morning, which is the most visible automation tell there is: the first batch
    # sent Quinton Components' sales manager and head of marketing subject lines that
    # differed by one word. So a company's second contact takes a DIFFERENT variant.
    #
    # This overrides the blind_spot routing rather than the requires_bi gate. The gate is a
    # truth constraint (blind_spot asserts they own a BI tool) and is never crossed; the
    # routing is only a preference, and the segment directive in every prompt still forbids
    # pitching a first dashboard to a company that already has one, whichever variant runs.
    assignments, rotation = [], 0
    used_by_company = {}
    for lead in leads:
        company = (lead.get("company_name") or "").strip().lower()
        used = used_by_company.setdefault(company, set())
        pool = eligible_variants(lead, variants)

        variant = pick_variant_for(lead, variants)
        if variant is not None and variant["id"] in used:
            variant = None  # a colleague already has it; fall through to the rotation
        if variant is None:
            fresh = [v for v in pool if v["id"] not in used] or pool
            variant = fresh[rotation % len(fresh)]
            rotation += 1

        used.add(variant["id"])
        assignments.append(variant)

    if args.dry_run:
        prompt_dir = outdir / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        for lead, variant in zip(leads, assignments):
            (prompt_dir / f"{slugify(lead['company_name'])}.txt").write_text(
                build_cold_prompt(lead, frameworks, variant)
            )
        counts = {}
        for variant in assignments:
            counts[variant["id"]] = counts.get(variant["id"], 0) + 1
        print(f"  [dry-run] {len(leads)} leads, wrote prompts to {prompt_dir}", flush=True)
        print(f"  [dry-run] frameworks: {', '.join(COLD_FRAMEWORKS)}", flush=True)
        print(f"  [dry-run] variant split: {counts}", flush=True)
        print("DRAFTED=0", flush=True)
        print(f"PROMPTS_PATH={prompt_dir}", flush=True)
        return 0

    # Generation is IO-bound: a draft is about a minute, essentially all of it spent waiting
    # on the model. Run them in a thread pool. Variants were assigned above, before any call
    # was made, so which lead gets which variant does not depend on completion order and the
    # batch is reproducible. Results are filled BY INDEX rather than appended, so the CSV
    # keeps the input's row order however the futures happen to land.
    slots = [None] * len(rows)
    done = 0
    # Set by the first worker to hit a spending cap or a dead key. Every other worker checks
    # it and returns immediately: without this, a capped project still walks the whole list
    # failing one lead at a time, which looks like progress and is not.
    fatal = threading.Event()
    fatal_message = []

    def work(index):
        nonlocal done
        if fatal.is_set():
            return
        row, lead, variant = rows[index], leads[index], assignments[index]
        try:
            draft, problems, attempts = generate_for_lead(
                lead, frameworks, known_tools, variant,
                model=args.model, exemplar_sents=exemplar_sents,
            )
        except FatalGenerationError as exc:
            if not fatal.is_set():
                fatal.set()
                fatal_message.append(str(exc))
            return
        except Exception as exc:  # a worker must never take the batch down with it
            draft, problems, attempts = None, [f"generation error: {exc}"], 0

        with PRINT_LOCK:
            done += 1
            position = f"{done:>3}/{len(rows)}"
            if draft is None:
                print(f"  [cold] {position} FAILED {lead['full_name']}: {problems}",
                      flush=True)
                return
            body = (draft.get("body") or "").strip()
            subject = (draft.get("subject") or "").strip()
            words = len(body.split())
            flag = f"  NEEDS REVIEW: {problems}" if problems else ""
            retry = "" if attempts == 1 else f" ({attempts} attempts)"
            print(
                f"  [cold] {position} {lead['full_name']:<20} {variant['id']:<15} "
                f"{words:>3}w {subject[:40]!r}{retry}{flag}",
                flush=True,
            )

        enriched_row = dict(row)
        enriched_row.update({
            "email_subject": subject,
            "personalized_email": body,
            "personalized_email_html": to_html(body),
            "email_variant": variant["id"],
            "email_word_count": words,
            "email_needs_review": "yes" if problems else "no",
        })
        slots[index] = (enriched_row, {
            "email": lead["email"],
            "first_name": lead["first_name"],
            "full_name": lead["full_name"],
            "company_name": lead["company_name"],
            "subject": subject,
            "body": body,
            "word_count": words,
            "variant_id": variant["id"],
            "named_tools": lead["named_tools"],
            "segment": lead["segment"],
            "lead_score": lead["lead_score"],
            "attempts": attempts,
            "needs_review": bool(problems),
            "problems": problems,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
        })

    # The money gate. Every path above this line is free: parsing, segmentation, variant
    # assignment and --dry-run all run without a single paid call, which is why the gate
    # sits here and not at the top of main().
    if not confirm_spend(calls=len(rows), model=args.model,
                         label="generate one cold email per lead",
                         assume_yes=args.yes_spend):
        return 1

    workers = max(1, min(args.workers, len(rows)))
    print(f"  [cold] generating {len(rows)} draft(s) across {workers} worker(s)", flush=True)
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, range(len(rows))))
    elapsed = time.time() - started

    out_rows = [slot[0] for slot in slots if slot]
    results = [slot[1] for slot in slots if slot]

    if fatal.is_set():
        print(f"\nERROR: the model API is refusing every request, so the batch was stopped "
              f"after {len(results)} draft(s). Nothing was written.", flush=True)
        print(f"  {fatal_message[0][:300]}", flush=True)
        print("  This is not a rate limit and waiting will not clear it. Fix the account "
              "or the key, then re-run: no partial CSV was left behind.", flush=True)
        return 1

    # A run that produced nothing must not write. An empty personalized CSV over a good one
    # destroys a batch that cost real money, and it exits 0, so the pipeline happily carries
    # it into the campaign stage. Same lesson as enrich_leads' coverage gate.
    if not results:
        print("\nERROR: no drafts were produced, so nothing was written.", flush=True)
        print("  Check the errors above before re-running.", flush=True)
        return 1

    print(f"  [cold] finished in {elapsed / 60:.1f} min "
          f"({elapsed / max(len(results), 1):.1f}s per draft)", flush=True)

    # Same columns in, plus the copy. utf-8-sig so Excel doesn't mangle the em dashes.
    csv_path = outdir / f"{leads_path.stem}_personalized.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames) + NEW_COLUMNS)
        writer.writeheader()
        writer.writerows(out_rows)

    drafts_path = outdir / "drafts.json"
    drafts_path.write_text(json.dumps(results, indent=2))

    clean = sum(1 for r in results if not r["needs_review"])
    print(f"DRAFTED={len(results)}", flush=True)
    print(f"CLEAN={clean}", flush=True)
    print(f"NEEDS_REVIEW={len(results) - clean}", flush=True)
    print(f"CSV_PATH={csv_path}", flush=True)
    print(f"DRAFTS_PATH={drafts_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
