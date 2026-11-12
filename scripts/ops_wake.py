"""Ops assistant wake: forward a compact notice of inbound Dialpad activity.

When an inbound SMS, inbound call, missed call, or voicemail reaches the
webhook server, this module POSTs a small JSON notice to an external wake URL
so an ops assistant can react to the event instead of polling Dialpad on a
timer.

Design invariants:
  * Opt-in. Nothing is sent unless OPS_WAKE_URL and OPS_WAKE_KEY are both set.
  * Never breaks the receiver. Every public entry point swallows its own
    errors, and the HTTP POST runs on a daemon thread after the caller returns.
  * Minimal payload. Event kind, our line, Dialpad ids, and the event time. No
    message bodies, caller numbers, transcripts, or recording links: the
    receiving assistant re-reads the event from Dialpad itself.
  * One wake per event. A persistent SQLite claim keyed on the Dialpad id and
    event kind absorbs Dialpad retries and repeated call-state events.
  * Bounded bursts. A per-minute and per-hour cap drops excess wakes; the next
    wake that does go out carries the number suppressed in between so the
    receiver knows to sweep.

Configuration (environment):
  OPS_WAKE_URL                 https URL to POST to (http only for loopback).
  OPS_WAKE_KEY                 bearer credential sent as Authorization header.
  OPS_WAKE_LINES               optional comma-separated E.164 lines to watch;
                               empty means every line.
  OPS_WAKE_MAX_PER_MINUTE      default 6.
  OPS_WAKE_MAX_PER_HOUR        default 40.
  OPS_WAKE_DEDUPE_DB           default /home/art/niemand/logs/ops_wake.db.
  OPS_WAKE_TIMEOUT_SECONDS     default 5.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DEDUPE_DB = "/home/art/niemand/logs/ops_wake.db"
DEDUPE_RETENTION_MS = 7 * 24 * 60 * 60 * 1000
USER_AGENT = "dialpad-ops-wake/1"

_rate_lock = threading.Lock()
_minute_window: deque = deque()
_hour_window: deque = deque()
_suppressed = 0
_db_lock = threading.Lock()


def _log(message):
    try:
        print(f"[{datetime.now().isoformat()}] ops-wake: {message}", flush=True)
    except Exception:  # noqa: BLE001 - logging must never raise.
        pass


def _int_env(name, default):
    try:
        value = int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _float_env(name, default):
    try:
        value = float(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _normalize_e164(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return None
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits


def _first(value):
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _clean_id(value):
    value = _first(value)
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def wake_url():
    return str(os.environ.get("OPS_WAKE_URL", "")).strip()


def wake_key():
    return str(os.environ.get("OPS_WAKE_KEY", "")).strip()


def _url_allowed(url):
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if not parsed.hostname:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def is_enabled():
    url = wake_url()
    return bool(url and wake_key() and _url_allowed(url))


def watched_lines():
    raw = str(os.environ.get("OPS_WAKE_LINES", ""))
    lines = {_normalize_e164(part) for part in raw.split(",")}
    lines.discard(None)
    return lines


def _line_watched(line):
    lines = watched_lines()
    if not lines:
        return True
    return line in lines


def _iso_from_ms(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    # Dialpad sends epoch milliseconds; tolerate seconds as well.
    if number < 1e11:
        number *= 1000
    try:
        return datetime.fromtimestamp(number / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def classify_call_event(data):
    """Return the wake kind for a Dialpad call event, or None to ignore it.

    Only inbound, external calls wake. `missed` is a missed call; `hangup`
    becomes a voicemail when a voicemail was left, an answered inbound call
    when it connected, and a missed call otherwise.
    """
    if not isinstance(data, dict):
        return None
    direction = str(data.get("direction") or data.get("call_direction") or "").lower()
    if direction != "inbound":
        return None
    if data.get("is_internal") is True:
        return None
    state = str(data.get("state") or data.get("call_state") or "").lower()
    has_voicemail = bool(data.get("voicemail_link") or data.get("voicemail_recording_id"))
    if state in {"voicemail", "voicemail_uploaded"}:
        return "voicemail"
    if state == "missed":
        return "voicemail" if has_voicemail else "missed_call"
    if state == "hangup":
        if has_voicemail:
            return "voicemail"
        try:
            talk = float(data.get("talk_time") or 0)
        except (TypeError, ValueError):
            talk = 0
        if data.get("date_connected") or talk > 0:
            return "inbound_call"
        return "missed_call"
    return None


def _call_line(data):
    target = data.get("target") if isinstance(data.get("target"), dict) else {}
    return _normalize_e164(
        _first(data.get("internal_number"))
        or target.get("phone")
        or _first(data.get("to_number"))
    )


def build_call_wake(data):
    kind = classify_call_event(data)
    if kind is None:
        return None
    call_id = _clean_id(data.get("call_id") or data.get("id"))
    if not call_id:
        return None
    return {
        "dedupe_key": f"call:{call_id}:{kind}",
        "payload": {
            "source": "dialpad",
            "event": kind,
            "line": _call_line(data),
            "occurred_at": _iso_from_ms(
                data.get("date_started") or data.get("date_rang") or data.get("event_timestamp")
            ),
            "dialpad": {
                "call_id": call_id,
                "entry_point_call_id": _clean_id(data.get("entry_point_call_id")),
                "master_call_id": _clean_id(data.get("master_call_id")),
                "state": str(data.get("state") or data.get("call_state") or "").lower() or None,
                "voicemail": kind == "voicemail",
            },
        },
    }


def build_sms_wake(data):
    if not isinstance(data, dict):
        return None
    if str(data.get("direction", "")).lower() != "inbound":
        return None
    if data.get("is_internal") is True:
        return None
    message_id = _clean_id(data.get("message_id") or data.get("id"))
    if not message_id:
        return None
    return {
        "dedupe_key": f"sms:{message_id}",
        "payload": {
            "source": "dialpad",
            "event": "inbound_sms",
            "line": _normalize_e164(_first(data.get("to_number"))),
            "occurred_at": _iso_from_ms(
                data.get("created_date") or data.get("event_timestamp") or data.get("timestamp")
            ),
            "dialpad": {
                "message_id": message_id,
                "conversation_id": _clean_id(data.get("conversation_id")),
                "mms": bool(data.get("mms")),
            },
        },
    }


def _dedupe_db_path():
    return Path(os.environ.get("OPS_WAKE_DEDUPE_DB", "").strip() or DEFAULT_DEDUPE_DB)


def claim_event(dedupe_key, *, now_ms=None, db_path=None):
    """Return True the first time `dedupe_key` is seen, False after that.

    Fails closed (returns False) when the store is unavailable: a missed wake
    is recoverable by a later sweep, a wake storm is not.
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    path = Path(db_path) if db_path is not None else _dedupe_db_path()
    try:
        with _db_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, timeout=5)
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS ops_wake_claims ("
                    "dedupe_key TEXT PRIMARY KEY, first_seen_at_ms INTEGER NOT NULL)"
                )
                conn.execute(
                    "DELETE FROM ops_wake_claims WHERE first_seen_at_ms < ?",
                    (now_ms - DEDUPE_RETENTION_MS,),
                )
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO ops_wake_claims (dedupe_key, first_seen_at_ms) VALUES (?, ?)",
                    (dedupe_key, now_ms),
                )
                conn.commit()
                return cursor.rowcount == 1
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001 - fail closed, never raise.
        _log(f"dedupe store unavailable ({type(exc).__name__}); wake skipped")
        return False


def _take_rate_slot(now=None):
    """Return (allowed, suppressed_before). Thread-safe sliding windows."""
    global _suppressed
    now = time.monotonic() if now is None else now
    per_minute = _int_env("OPS_WAKE_MAX_PER_MINUTE", 6)
    per_hour = _int_env("OPS_WAKE_MAX_PER_HOUR", 40)
    with _rate_lock:
        while _minute_window and now - _minute_window[0] >= 60:
            _minute_window.popleft()
        while _hour_window and now - _hour_window[0] >= 3600:
            _hour_window.popleft()
        if len(_minute_window) >= per_minute or len(_hour_window) >= per_hour:
            _suppressed += 1
            return False, 0
        _minute_window.append(now)
        _hour_window.append(now)
        suppressed, _suppressed = _suppressed, 0
        return True, suppressed


def reset_state_for_tests():
    global _suppressed
    with _rate_lock:
        _minute_window.clear()
        _hour_window.clear()
        _suppressed = 0


def post_wake(payload, *, url=None, key=None, timeout=None):
    """POST one wake. Returns True on a 2xx. Never raises."""
    url = url or wake_url()
    key = key or wake_key()
    timeout = timeout or _float_env("OPS_WAKE_TIMEOUT_SECONDS", 5)
    host = urllib.parse.urlsplit(url).hostname or "?"
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
        ok = 200 <= int(status) < 300
        _log(f"{payload.get('event')} -> {host} status={status}")
        return ok
    except urllib.error.HTTPError as exc:
        _log(f"{payload.get('event')} -> {host} http_error={exc.code}")
    except Exception as exc:  # noqa: BLE001 - network failures stay local.
        _log(f"{payload.get('event')} -> {host} failed ({type(exc).__name__})")
    return False


def _dispatch(wake, *, background=True):
    if not wake:
        return False
    payload = wake["payload"]
    if not _line_watched(payload.get("line")):
        return False
    if not claim_event(wake["dedupe_key"]):
        return False
    allowed, suppressed = _take_rate_slot()
    if not allowed:
        _log(f"{payload.get('event')} rate-capped; will report on next wake")
        return False
    payload = dict(payload)
    payload["sent_at"] = _now_iso()
    payload["suppressed_since_last"] = suppressed
    url, key = wake_url(), wake_key()
    if background:
        thread = threading.Thread(
            target=post_wake, args=(payload,), kwargs={"url": url, "key": key}, daemon=True,
            name="ops-wake",
        )
        thread.start()
    else:
        post_wake(payload, url=url, key=key)
    return True


def notify_call_event(data, *, background=True):
    """Queue a wake for a Dialpad call event. Returns True if one was queued."""
    try:
        if not is_enabled():
            return False
        return _dispatch(build_call_wake(data), background=background)
    except Exception as exc:  # noqa: BLE001 - never break the receiver.
        _log(f"call wake skipped ({type(exc).__name__})")
        return False


def notify_sms_event(data, *, background=True):
    """Queue a wake for an inbound Dialpad SMS. Returns True if one was queued."""
    try:
        if not is_enabled():
            return False
        return _dispatch(build_sms_wake(data), background=background)
    except Exception as exc:  # noqa: BLE001 - never break the receiver.
        _log(f"sms wake skipped ({type(exc).__name__})")
        return False
