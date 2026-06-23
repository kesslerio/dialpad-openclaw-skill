#!/usr/bin/env python3
"""Calendar context adapter for the Dialpad auto-responder (S1/U3).

Resolves an upcoming demo time for the calendar-aware draft mode. The Attio deal
``demo_scheduled_at`` attribute is the reliable path; Calendly is best-effort and
only fires when an invitee email is present in the query (its ``invitee_email``
filter behavior is unconfirmed — see docs/reference/attio-schema.md and the S1
plan). Wired as a context command:

    DIALPAD_CALENDAR_CONTEXT_COMMAND="/abs/python3 /abs/scripts/adapters/calendar_context.py"

Query (single final CLI arg, space-joined): "<name> <company> <deal> <timestamp>"
Emits the contract consumed by ``lookup_sales_calendar_context``:

    {"usable": true, "status": "ok", "basis": "attio",
     "summary": "...", "startsInMinutes": 42}

Future demos and bounded recent demos are surfaced; stale/past dates outside the
lookback window return ``{"usable": false}``.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import attio_context as attio  # noqa: E402  (sibling adapter = shared Attio client)

CALENDLY_BASE = os.environ.get("CALENDLY_API_BASE", "https://api.calendly.com").rstrip("/")
CALENDLY_TIMEOUT = float(os.environ.get("CALENDLY_HTTP_TIMEOUT_SECONDS", "2.5"))
DEFAULT_RECENT_DEMO_LOOKBACK_MINUTES = 7 * 24 * 60


def parse_int_env(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


RECENT_DEMO_LOOKBACK_MINUTES = parse_int_env(
    "DIALPAD_RECENT_DEMO_LOOKBACK_MINUTES",
    DEFAULT_RECENT_DEMO_LOOKBACK_MINUTES,
)

# Trailing timestamp the webhook appends (ISO 8601 or unix epoch).
_TIMESTAMP_RE = re.compile(r"\s*(?:\d{4}-\d{2}-\d{2}[T ]\S+|\b\d{10}\b)\s*$")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_MIN_TOKEN = 4


def _now():
    return datetime.now(timezone.utc)


def parse_iso(value):
    """Parse an ISO 8601 timestamp/date (Attio uses 9 fractional digits) to aware UTC, or None."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)  # trim sub-microsecond precision
    for candidate in (text, text[:10]):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except ValueError:
            continue
    return None


def starts_in_minutes(dt, now=None):
    if dt is None:
        return None
    return int((dt - (now or _now())).total_seconds() // 60)


def _search_token(remainder):
    """Pick the most distinctive alphabetic token to match a deal name against."""
    tokens = sorted(
        (t for t in re.findall(r"[A-Za-z][A-Za-z'&]+", remainder) if len(t) >= _MIN_TOKEN),
        key=len,
        reverse=True,
    )
    return tokens[0] if tokens else None


def _demo_timestamp(deal):
    values = (deal or {}).get("values") or {}
    return attio._text_value(values, "demo_scheduled_at") or attio._text_value(values, "demo_scheduled_date")


def resolve_attio_demo(remainder, now=None):
    """Return (startsInMinutes, summary, demoState) from one confident Attio deal match.

    Conservative by design: 0 or multiple name matches → not usable, so the draft
    falls back to generic rather than guessing a meeting time.
    """
    token = _search_token(remainder)
    if not token:
        return None, None, None
    try:
        deals = attio._query_records("deals", {"name": {"$contains": token}}, limit=3)
    except attio.AttioError:
        return None, None, None
    if len(deals) != 1:
        return None, None, None
    deal = deals[0]
    sim = starts_in_minutes(parse_iso(_demo_timestamp(deal)), now=now)
    if sim is None:
        return None, None, None
    name = attio._text_value((deal.get("values") or {}), "name") or "your demo"
    clean_name = attio._clean(name)
    if sim < 0:
        if abs(sim) > RECENT_DEMO_LOOKBACK_MINUTES:
            return None, None, None
        return abs(sim), f"Recent demo: {clean_name}", "recent"
    return sim, f"Upcoming demo: {clean_name}", "upcoming"


def calendly_next_event(email, now=None):
    """Best-effort: (startsInMinutes, summary) for an invitee's next active Calendly event, else (None, None)."""
    api_key = os.environ.get("CALENDLY_API_KEY", "")
    if not api_key or not email:
        return None, None
    now = now or _now()

    def _get(url):
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=CALENDLY_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    try:
        me = _get(f"{CALENDLY_BASE}/users/me")
        org = ((me.get("resource") or {}).get("current_organization"))
        if not (isinstance(org, str) and org.startswith(f"{CALENDLY_BASE}/organizations/")):
            return None, None
        qs = urlencode({
            "organization": org,
            "invitee_email": email,
            "status": "active",
            "min_start_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sort": "start_time:asc",
            "count": 1,
        })
        events = _get(f"{CALENDLY_BASE}/scheduled_events?{qs}").get("collection") or []
        if not events:
            return None, None
        ev = events[0]
        sim = starts_in_minutes(parse_iso(ev.get("start_time")), now=now)
        if sim is None or sim < 0:
            return None, None
        return sim, f"Upcoming demo: {attio._clean(ev.get('name')) or 'scheduled meeting'}"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None, None


def build_calendar_context(query, now=None):
    raw = str(query or "").strip()
    if not raw:
        return {"usable": False, "status": "empty_query"}
    remainder = _TIMESTAMP_RE.sub("", raw).strip()

    sim, summary, demo_state, basis = (*resolve_attio_demo(remainder, now=now), "attio")
    if sim is None and os.environ.get("CALENDLY_API_KEY"):
        email_match = _EMAIL_RE.search(remainder)
        if email_match:
            sim, summary = calendly_next_event(email_match.group(0), now=now)
            demo_state = "upcoming" if sim is not None else None
            basis = "calendly"

    if sim is None or not summary:
        return {"usable": False, "status": "not_found"}
    return {
        "usable": True,
        "status": "ok",
        "basis": basis,
        "summary": summary,
        "startsInMinutes": sim,
        "demoState": demo_state or "upcoming",
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    query = argv[-1] if argv else ""
    try:
        result = build_calendar_context(query)
    except Exception:  # noqa: BLE001 - fail closed; the adapter must never break the webhook
        result = {"usable": False, "status": "error"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
