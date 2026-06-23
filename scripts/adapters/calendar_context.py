#!/usr/bin/env python3
"""Calendar context adapter for the Dialpad auto-responder (S1/U3).

Resolves an upcoming demo time for the calendar-aware draft mode. ShapeScale
Google Calendar via ``DIALPAD_GOG_CALENDAR_COMMAND`` is the preferred source of
truth for actual scheduled events. The Attio deal ``demo_scheduled_at`` attribute
is the structured fallback; Calendly is best-effort and only fires when an
invitee email is present in the query. Wired as a context command:

    DIALPAD_CALENDAR_CONTEXT_COMMAND="/abs/python3 /abs/scripts/adapters/calendar_context.py"

Query (single final CLI arg, space-joined): "<name> <email> <company> <deal> <timestamp>"
Emits the contract consumed by ``lookup_sales_calendar_context``:

    {"usable": true, "status": "ok", "basis": "attio",
     "summary": "...", "startsInMinutes": 42}

Future demos and bounded recent demos are surfaced; stale/past dates outside the
lookback window return ``{"usable": false}``.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import attio_context as attio  # noqa: E402  (sibling adapter = shared Attio client)

CALENDLY_BASE = os.environ.get("CALENDLY_API_BASE", "https://api.calendly.com").rstrip("/")
CALENDLY_TIMEOUT = float(os.environ.get("CALENDLY_HTTP_TIMEOUT_SECONDS", "2.5"))
GOG_CALENDAR_TIMEOUT = float(os.environ.get("DIALPAD_GOG_CALENDAR_TIMEOUT_SECONDS", "2.5"))
DEFAULT_RECENT_DEMO_LOOKBACK_MINUTES = 7 * 24 * 60
DEFAULT_GOG_LOOKAHEAD_DAYS = 120
DEFAULT_GOG_CALENDAR_IDS = "primary,alex@shapescale.com,lilla@shapescale.com"


def parse_int_env(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


RECENT_DEMO_LOOKBACK_MINUTES = parse_int_env(
    "DIALPAD_RECENT_DEMO_LOOKBACK_MINUTES",
    DEFAULT_RECENT_DEMO_LOOKBACK_MINUTES,
)
GOG_LOOKAHEAD_DAYS = parse_int_env("DIALPAD_GOG_CALENDAR_LOOKAHEAD_DAYS", DEFAULT_GOG_LOOKAHEAD_DAYS)
GOG_CALENDAR_COMMAND = os.environ.get("DIALPAD_GOG_CALENDAR_COMMAND", "")
GOG_CALENDAR_ACCOUNT = os.environ.get("DIALPAD_GOG_CALENDAR_ACCOUNT", "martin@shapescale.com")
GOG_CALENDAR_ID = os.environ.get("DIALPAD_GOG_CALENDAR_ID", "primary")
GOG_CALENDAR_IDS = os.environ.get("DIALPAD_GOG_CALENDAR_IDS", DEFAULT_GOG_CALENDAR_IDS)
GOG_CALENDAR_MAX_RESULTS = parse_int_env("DIALPAD_GOG_CALENDAR_MAX_RESULTS", 250)

# Trailing timestamp the webhook appends (ISO 8601 or unix epoch).
_TIMESTAMP_RE = re.compile(r"\s*(?:\d{4}-\d{2}-\d{2}[T ]\S+|\b\d{10}\b)\s*$")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_MIN_TOKEN = 4
_GOG_TOKEN_STOPWORDS = {
    "booked",
    "booking",
    "call",
    "demo",
    "inbound",
    "meeting",
    "request",
    "scheduled",
    "shapescale",
    "shape",
}


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


def _iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_start(event):
    start = (event or {}).get("start")
    if isinstance(start, dict):
        return parse_iso(start.get("dateTime") or start.get("date"))
    return parse_iso(start)


def _event_match_text(event):
    pieces = [
        event.get("summary"),
        event.get("description"),
        event.get("location"),
    ]
    for attendee in event.get("attendees") or []:
        if isinstance(attendee, dict):
            pieces.append(attendee.get("email"))
            pieces.append(attendee.get("displayName"))
    return " ".join(str(piece or "") for piece in pieces).lower()


def _gog_match_terms(remainder):
    emails = [email.lower() for email in _EMAIL_RE.findall(remainder or "")]
    tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z'&]+", remainder or "")
        if len(token) >= _MIN_TOKEN and token.lower() not in _GOG_TOKEN_STOPWORDS
    }
    return emails, tokens


def _gog_event_score(event, emails, tokens):
    text = _event_match_text(event)
    score = 0
    if emails and any(email in text for email in emails):
        score += 5
    token_hits = [token for token in tokens if token in text]
    score += len(token_hits)
    if any(len(token) >= 8 for token in token_hits):
        score += 1
    return score


def _gog_calendar_ids():
    configured = str(GOG_CALENDAR_IDS or "").strip()
    if not configured:
        configured = str(GOG_CALENDAR_ID or "").strip()
    calendars = [calendar_id.strip() for calendar_id in configured.split(",") if calendar_id.strip()]
    return calendars or ["primary"]


def _gog_calendar_label(calendar_id):
    if calendar_id == "primary" or calendar_id == "martin@shapescale.com":
        return "Work"
    if calendar_id == "alex@shapescale.com":
        return "Alex"
    if calendar_id == "lilla@shapescale.com":
        return "Lilla"
    return calendar_id


def _gog_events_for_calendar(command, calendar_id, now):
    start = now - timedelta(minutes=max(0, RECENT_DEMO_LOOKBACK_MINUTES))
    try:
        args = shlex.split(command) + [
            "calendar",
            "events",
            calendar_id,
            "--from",
            _iso_z(start),
            "--to",
            _iso_z(now + timedelta(days=max(1, GOG_LOOKAHEAD_DAYS))),
            "--max",
            str(max(1, GOG_CALENDAR_MAX_RESULTS)),
            "--account",
            GOG_CALENDAR_ACCOUNT,
            "--json",
            "--results-only",
            "--no-input",
        ]
    except ValueError:
        return "invalid_command", []
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=GOG_CALENDAR_TIMEOUT,
        )
    except FileNotFoundError:
        return "unavailable", []
    except subprocess.TimeoutExpired:
        return "timeout", []
    except OSError:
        return "unavailable", []
    if completed.returncode != 0:
        return f"exit_{completed.returncode}", []
    try:
        events = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        return "unavailable", []
    if isinstance(events, dict):
        events = events.get("events") or events.get("items") or events.get("collection") or []
    if not isinstance(events, list):
        return "unavailable", []
    return "ok", events


def gog_next_event(remainder, now=None):
    """Best-effort: demo event from the ShapeScale work calendars.

    Returns ``(startsInMinutes, summary, demoState, status)``. Recent demos use
    positive minutes-since-start for parity with the Attio fallback.
    """
    command = str(GOG_CALENDAR_COMMAND or "").strip()
    if not command:
        return None, None, None, "not_configured"
    emails, tokens = _gog_match_terms(remainder)
    if not emails and not tokens:
        return None, None, None, "not_found"
    now = now or _now()

    best = None
    best_key = None
    failure_status = None
    for calendar_id in _gog_calendar_ids():
        status, events = _gog_events_for_calendar(command, calendar_id, now)
        if status != "ok":
            failure_status = failure_status or status
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            start = _event_start(event)
            sim = starts_in_minutes(start, now=now)
            if sim is None:
                continue
            if sim < 0 and abs(sim) > RECENT_DEMO_LOOKBACK_MINUTES:
                continue
            score = _gog_event_score(event, emails, tokens)
            if score < 2:
                continue
            is_upcoming = 1 if sim >= 0 else 0
            closeness = -sim if sim >= 0 else sim
            key = (score, is_upcoming, closeness)
            if best is None or key > best_key:
                best = (sim, event.get("summary") or "scheduled meeting", _gog_calendar_label(calendar_id))
                best_key = key
    if best is None:
        return None, None, None, failure_status or "not_found"
    sim, summary, calendar_label = best
    clean_summary = attio._clean(summary) or "scheduled meeting"
    if sim < 0:
        return abs(sim), f"Recent demo: {clean_summary} ({calendar_label})", "recent", "ok"
    return sim, f"Upcoming demo: {clean_summary} ({calendar_label})", "upcoming", "ok"


def resolve_attio_demo(remainder, now=None):
    """Return (startsInMinutes, summary, demoState) from one confident Attio deal match.

    Conservative by design: 0 or multiple name matches → not usable, so the draft
    falls back to generic rather than guessing a meeting time.
    """
    token = _search_token(_EMAIL_RE.sub(" ", str(remainder or "")))
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

    sim, summary, demo_state, gog_status = gog_next_event(remainder, now=now)
    basis = "google_calendar"
    if sim is None:
        sim, summary, demo_state, basis = (*resolve_attio_demo(remainder, now=now), "attio")
    if sim is None and os.environ.get("CALENDLY_API_KEY"):
        email_match = _EMAIL_RE.search(remainder)
        if email_match:
            sim, summary = calendly_next_event(email_match.group(0), now=now)
            demo_state = "upcoming" if sim is not None else None
            basis = "calendly"

    if sim is None or not summary:
        return {"usable": False, "status": gog_status if gog_status != "not_found" else "not_found"}
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
