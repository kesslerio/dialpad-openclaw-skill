#!/usr/bin/env python3
"""Poll Dialpad inbound calls for new voicemails and notify Telegram."""

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DIALPAD_API_KEY = os.environ.get("DIALPAD_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("DIALPAD_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("DIALPAD_TELEGRAM_CHAT_ID", "")
DIALPAD_LINE_NAMES = os.environ.get("DIALPAD_LINE_NAMES", "")

DEFAULT_LINE_NAMES = {
    "+14155201316": "Sales",
    "+14153602954": "Work",
    "+14159917155": "Support",
}

LOOKBACK_HOURS_RAW = os.environ.get("POLL_LOOKBACK_HOURS", "2")
DB_PATH_RAW = os.environ.get(
    "VOICEMAIL_DB_PATH",
    str(Path.home() / ".local" / "share" / "dialpad" / "voicemails_seen.db"),
)


def normalize_phone_number(phone_number):
    """
    Normalize a phone number to last 10 digits for reliable comparisons.
    Removes non-digits, optional leading country code 1, and keeps last 10 digits.
    """
    if not phone_number:
        return None

    digits = "".join(ch for ch in str(phone_number) if ch.isdigit())
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def format_phone_number(phone_number):
    """Format normalized digits as (NXX) NXX-XXXX when possible."""
    normalized = normalize_phone_number(phone_number)
    if not normalized:
        return None
    if len(normalized) == 10:
        return f"({normalized[:3]}) {normalized[3:6]}-{normalized[6:]}"
    return normalized


def load_line_names():
    """
    Load line-name mapping from env and merge with defaults.
    Env values override defaults, defaults still act as fallback.
    """
    loaded = {}
    for number, name in DEFAULT_LINE_NAMES.items():
        normalized = normalize_phone_number(number)
        if normalized:
            loaded[normalized] = str(name)

    if not DIALPAD_LINE_NAMES:
        return loaded

    try:
        env_mapping = json.loads(DIALPAD_LINE_NAMES)
        if not isinstance(env_mapping, dict):
            raise ValueError("DIALPAD_LINE_NAMES must be a JSON object")
        for number, name in env_mapping.items():
            normalized = normalize_phone_number(number)
            if normalized and name:
                loaded[normalized] = str(name)
    except Exception as exc:
        print(f"⚠️  Invalid DIALPAD_LINE_NAMES, using defaults: {exc}", file=sys.stderr)

    return loaded


LINE_NAMES = load_line_names()


def get_line_name(to_number):
    """
    Resolve a Dialpad receiving line number to display text.
    Returns "Friendly Name (NXX) NXX-XXXX" when mapped, "(NXX) NXX-XXXX"
    when not mapped, and None when to_number is missing.
    """
    normalized = normalize_phone_number(to_number)
    if not normalized:
        return None

    formatted = format_phone_number(normalized) or normalized
    friendly = LINE_NAMES.get(normalized)
    if friendly:
        return f"{friendly} {formatted}"
    return formatted


def escape_markdown(text):
    """Escape Telegram Markdown special chars for parse_mode=Markdown."""
    escaped = str(text)
    for ch in ("_", "*", "`", "["):
        escaped = escaped.replace(ch, f"\\{ch}")
    return escaped


def parse_positive_float(raw, default):
    try:
        value = float(raw)
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def ensure_db(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voicemails_seen (
            call_id TEXT PRIMARY KEY,
            notified_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def has_seen(conn, call_id):
    cursor = conn.execute("SELECT 1 FROM voicemails_seen WHERE call_id = ?", (call_id,))
    return cursor.fetchone() is not None


def mark_seen(conn, call_id):
    conn.execute(
        "INSERT OR IGNORE INTO voicemails_seen (call_id, notified_at) VALUES (?, ?)",
        (call_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def send_to_telegram(text):
    """
    Send a message to the configured Telegram channel.
    Returns True on success, False on failure (non-blocking).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram not configured (missing BOT_TOKEN or CHAT_ID)", file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as exc:
        print(f"❌ Error sending to Telegram: {exc}", file=sys.stderr)
        return False


def fetch_inbound_calls(api_key, lookback_ms=None, now_ms=None):
    """Fetch inbound calls, paginating until items are older than lookback window."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    all_calls = []
    cursor = None
    max_pages = 20  # safety cap (~1000 calls max)

    for _ in range(max_pages):
        url = "https://dialpad.com/api/v2/call?direction=inbound&limit=50"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))

        if isinstance(data, list):
            all_calls.extend(data)
            break

        if not isinstance(data, dict):
            break

        items = []
        for key in ("items", "calls", "data", "results"):
            value = data.get(key)
            if isinstance(value, list):
                items = value
                break

        all_calls.extend(items)

        # Stop paginating if all items on this page are older than lookback
        if lookback_ms and now_ms and items:
            oldest = min(
                int(float(c.get("date_ended") or 0)) for c in items
            )
            if oldest < (now_ms - lookback_ms):
                break

        cursor = data.get("cursor")
        if not cursor or not items:
            break

    return all_calls


def has_voicemail(call):
    link = str(call.get("voicemail_link") or "").strip()
    recording_id = str(call.get("voicemail_recording_id") or "").strip()
    return bool(link or recording_id)


def is_within_lookback(call, lookback_ms, now_ms):
    ended_raw = call.get("date_ended")
    try:
        ended_ms = int(float(str(ended_raw)))
    except (TypeError, ValueError):
        return False

    return (now_ms - lookback_ms) <= ended_ms <= now_ms


def looks_like_phone(value):
    normalized = normalize_phone_number(value)
    if not normalized:
        return False
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return len(digits) >= 7


def clean_contact_name(name, from_num):
    if not name:
        return None

    candidate = str(name).strip()
    if not candidate:
        return None

    if looks_like_phone(candidate):
        candidate_norm = normalize_phone_number(candidate)
        from_norm = normalize_phone_number(from_num)
        if candidate_norm == from_norm:
            return None

    return candidate


def format_duration(call):
    raw_ms = call.get("total_duration")
    if raw_ms is None:
        return "0s"

    try:
        seconds = int(round(float(raw_ms) / 1000.0))
    except (TypeError, ValueError):
        seconds = 0

    if seconds < 0:
        seconds = 0
    return f"{seconds}s"


def build_notification(call):
    from_num = str(call.get("external_number") or "Unknown")
    to_num = call.get("internal_number")

    to_display = get_line_name(to_num)
    if not to_display:
        to_display = str(to_num or "Unknown")

    contact_name = clean_contact_name((call.get("contact") or {}).get("name"), from_num)
    if contact_name:
        from_display = f"*{escape_markdown(contact_name)}* (`{escape_markdown(from_num)}`)"
    else:
        from_display = f"`{escape_markdown(from_num)}`"

    text = (
        "📬 *New Voicemail*\n"
        f"*To:* {escape_markdown(to_display)}\n"
        f"*From:* {from_display}\n"
        f"*Duration:* {format_duration(call)}"
    )

    transcription = str(call.get("transcription_text") or "").strip()
    if transcription:
        text += (
            "\n\n"
            "*Transcription:*\n"
            f"_\"{escape_markdown(transcription)}\"_"
        )

    return text


def main():
    found_count = 0
    notified_count = 0

    try:
        lookback_hours = parse_positive_float(LOOKBACK_HOURS_RAW, 2.0)
        lookback_ms = int(lookback_hours * 60 * 60 * 1000)

        if not DIALPAD_API_KEY:
            print("❌ Missing required env var: DIALPAD_API_KEY", file=sys.stderr)
            print("found 0 voicemail(s), notified 0 new")
            return 0

        db_path = Path(DB_PATH_RAW).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)

        now_ms = int(time.time() * 1000)

        with sqlite3.connect(str(db_path)) as conn:
            ensure_db(conn)

            try:
                calls = fetch_inbound_calls(DIALPAD_API_KEY, lookback_ms=lookback_ms, now_ms=now_ms)
            except urllib.error.HTTPError as exc:
                print(f"❌ Dialpad API HTTP error: {exc.code} {exc.reason}", file=sys.stderr)
                print("found 0 voicemail(s), notified 0 new")
                return 0
            except Exception as exc:
                print(f"❌ Dialpad API request failed: {exc}", file=sys.stderr)
                print("found 0 voicemail(s), notified 0 new")
                return 0
            voicemails = [
                call
                for call in calls
                if isinstance(call, dict)
                and has_voicemail(call)
                and is_within_lookback(call, lookback_ms, now_ms)
            ]
            found_count = len(voicemails)

            for call in voicemails:
                call_id = str(call.get("call_id") or "").strip()
                if not call_id:
                    print("⚠️  Skipping voicemail with missing call_id", file=sys.stderr)
                    continue
                if has_seen(conn, call_id):
                    continue

                message = build_notification(call)
                if send_to_telegram(message):
                    mark_seen(conn, call_id)
                    notified_count += 1

    except Exception as exc:
        print(f"❌ Poller error: {exc}", file=sys.stderr)

    print(f"found {found_count} voicemail(s), notified {notified_count} new")
    return 0


if __name__ == "__main__":
    sys.exit(main())
