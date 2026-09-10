"""
Layer 3 execution tool: mock of RB2B's person-level identity graph.

>>> THIS IS THE ONLY MOCKED COMPONENT. Delete it on cutover to a real RB2B account. <<<

RB2B's paid product does one thing we cannot replicate locally: it takes an anonymous
pageview and resolves it to a named person. Everything else in this pipeline —
the pixel call site, the webhook payload, the receiver, enrichment, drafting — is real.
This file stands in for that one step, and deliberately reproduces RB2B's *behaviour*
around it so the rest of the system is exercised the way production would exercise it:

  - US-only. RB2B resolves US traffic; everything else is dropped unresolved.
  - Partial match rate. Not every visitor resolves. Config `match_rate` (1.0 for the
    demo so all 10 land; ~0.3 is realistic).
  - Initial visit only. A person fires once, not once per page, unless the
    "Send repeat visitor data" toggle is on (`send_repeat_visits`).
  - Asynchronous, with latency. The match happens after the pageview, not during it,
    so the webhook arrives seconds later on a separate connection.
  - RB2B-exact payload. Space-separated Title Case keys, nullable fields left null,
    "Employee Count" emitted as both int and string across the batch because RB2B
    types it as either.

Geo note: real RB2B geolocates the visitor's IP before attempting a match. The mock
reads the drawn persona's `country` instead, which is equivalent for our purposes and
keeps the fiction contained in the file that gets deleted.

Importable:
    from rb2b_mock_resolver import MockResolver
    resolver = MockResolver(config, webhook_url="http://127.0.0.1:5000/rb2b/webhook")
    resolver.handle(pageview_event)   # fires a webhook in the background, or skips
    resolver.join()                   # wait for in-flight webhooks to land
"""

import json
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import ROOT  # noqa: E402  (also puts every execution area on sys.path)

# Data that belongs to this area lives beside it.
HERE = Path(__file__).resolve().parent
PERSONAS_PATH = HERE / "mock_data" / "personas.json"


def log(msg):
    # flush=True or nothing: the site is normally run backgrounded with stdout
    # redirected to a file, where buffered prints never appear.
    print(msg, flush=True)


def load_personas(path=None, include_international=False):
    data = json.loads(Path(path or PERSONAS_PATH).read_text())
    people = data["personas"]
    if not include_international:
        people = [p for p in people if p.get("country") == "US"]
    return people


class MockResolver:
    def __init__(
        self,
        config,
        webhook_url,
        personas=None,
        seed=7,
        include_international=True,
        verbose=True,
    ):
        self.config = config
        self.webhook_url = webhook_url
        self.token = config.get("webhook_token", "")
        self.match_rate = float(config.get("match_rate", 1.0))
        self.us_only = bool(config.get("us_only", True))
        self.send_repeat_visits = bool(config.get("send_repeat_visits", False))
        self.delay_range = config.get("resolver_delay_seconds", [0.3, 1.2])
        self.verbose = verbose

        # include_international governs the POOL, not the filter. The pool holds the
        # non-US personas so the us_only filter has something to actually reject.
        self.pool = personas if personas is not None else load_personas(
            include_international=include_international
        )
        self._rng = random.Random(seed)
        self._order = list(range(len(self.pool)))
        self._rng.shuffle(self._order)

        self._lock = threading.Lock()
        self._assigned = {}    # visitor_id -> persona
        self._fired = set()    # visitor_ids already sent to the webhook
        self._next = 0
        self._threads = []
        self.stats = {
            "pageviews": 0,
            "assigned": 0,
            "skipped_pool_exhausted": 0,
            "skipped_non_us": 0,
            "skipped_repeat_visit": 0,
            "skipped_no_match": 0,
            "fired": 0,
            "errors": 0,
        }

    # ---------------------------------------------------------------- assignment

    def _assign(self, visitor_id):
        """First time we see an anonymous visitor, draw the next persona for them.

        Stands in for the identity graph's cookie/IP resolution. Deterministic given
        the seed, so a run is reproducible.
        """
        with self._lock:
            if visitor_id in self._assigned:
                return self._assigned[visitor_id]
            if self._next >= len(self._order):
                return None
            persona = self.pool[self._order[self._next]]
            self._next += 1
            self._assigned[visitor_id] = persona
            self.stats["assigned"] += 1
            return persona

    # ------------------------------------------------------------------- payload

    def build_payload(self, persona, event, is_repeat_visit=False):
        """An RB2B webhook body, field for field.

        Nulls are emitted as real nulls rather than omitted, and Employee Count
        alternates int/string across the batch, because RB2B does both and a receiver
        that only ever sees one shape isn't really tested.
        """
        emp = persona.get("employee_count")
        as_string = (hash(persona["id"]) % 2) == 0
        employee_count = str(emp) if (as_string and emp is not None) else emp

        return {
            "LinkedIn URL": persona["linkedin_url"],
            "First Name": persona["first_name"],
            "Last Name": persona.get("last_name"),
            "Title": persona.get("title"),
            "Company Name": persona.get("company_name"),
            "Business Email": persona.get("business_email"),
            "Website": persona.get("website"),
            "Industry": persona.get("industry"),
            "Employee Count": employee_count,
            "Estimate Revenue": persona.get("estimated_revenue"),
            "City": persona.get("city"),
            "State": persona.get("state"),
            "Zipcode": persona.get("zipcode"),
            "Seen At": datetime.now(timezone.utc).isoformat(),
            "Referrer": event.get("referrer"),
            "Captured URL": event.get("captured_url"),
            "Tags": None,
            "is_repeat_visit": is_repeat_visit,
        }

    # -------------------------------------------------------------------- firing

    def _post(self, payload, delay):
        import requests

        time.sleep(delay)
        try:
            resp = requests.post(
                self.webhook_url,
                params={"token": self.token},
                json=payload,
                timeout=10,
            )
            with self._lock:
                if resp.ok:
                    self.stats["fired"] += 1
                else:
                    self.stats["errors"] += 1
            if self.verbose:
                who = f"{payload['First Name']} {payload.get('Last Name') or ''}".strip()
                log(f"  [resolver] {resp.status_code} {who} — {payload.get('Company Name')}")
        except Exception as exc:  # a resolver failure must not take the site down
            with self._lock:
                self.stats["errors"] += 1
            if self.verbose:
                log(f"  [resolver] ERROR posting webhook: {exc}")

    def handle(self, event):
        """Consume one pageview beacon. Fires a webhook in the background, or skips."""
        with self._lock:
            self.stats["pageviews"] += 1

        visitor_id = event.get("visitor_id") or "v-unknown"
        persona = self._assign(visitor_id)
        if persona is None:
            with self._lock:
                self.stats["skipped_pool_exhausted"] += 1
            return

        if self.us_only and persona.get("country") != "US":
            with self._lock:
                self.stats["skipped_non_us"] += 1
            if self.verbose:
                log(f"  [resolver] skip (non-US: {persona.get('country')}) {persona['first_name']}")
            return

        with self._lock:
            already = visitor_id in self._fired
        if already and not self.send_repeat_visits:
            with self._lock:
                self.stats["skipped_repeat_visit"] += 1
            return

        # Match-rate sampling, deterministic per visitor so re-runs are reproducible.
        if not already:
            if random.Random(f"{visitor_id}:match").random() > self.match_rate:
                with self._lock:
                    self.stats["skipped_no_match"] += 1
                if self.verbose:
                    log(f"  [resolver] skip (no match) {persona['first_name']}")
                return

        with self._lock:
            self._fired.add(visitor_id)

        payload = self.build_payload(persona, event, is_repeat_visit=already)
        delay = random.uniform(*self.delay_range)
        thread = threading.Thread(target=self._post, args=(payload, delay), daemon=True)
        thread.start()
        with self._lock:
            self._threads.append(thread)

    def join(self, timeout=30):
        """Block until every in-flight webhook has landed (or timed out)."""
        deadline = time.time() + timeout
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            remaining = max(0.1, deadline - time.time())
            thread.join(timeout=remaining)

    def summary(self):
        return dict(self.stats)
