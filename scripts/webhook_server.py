#!/usr/bin/env python3
"""
Dialpad SMS Webhook Server with SQLite Storage and OpenClaw Hooks Ingress

Receives Dialpad SMS events, stores in SQLite with FTS5 search, and sends
OpenClaw hook messages for inbound SMS and missed calls.

Features:
- SQLite storage with FTS5 full-text search (via webhook_sqlite.py)
- OpenClaw hooks forwarding for inbound SMS and missed calls
- Telegram notifications for missed calls and voicemails
- Health check endpoint
- Graceful error handling (hook/Telegram failures don't break webhooks)
- Zero external dependencies (stdlib only)
- All secrets from environment variables

Environment Variables:
- PORT (default: 8081) - HTTP server port
- DIALPAD_TELEGRAM_BOT_TOKEN - Telegram bot token (required for call/voicemail notifications)
- DIALPAD_TELEGRAM_CHAT_ID - Telegram chat ID (required for call/voicemail notifications)
- DIALPAD_TELEGRAM_APPROVAL_BUTTONS_ENABLED (default: disabled)
- TELEGRAM_WEBHOOK_SECRET - Telegram secret_token for inline approval callbacks
- DIALPAD_API_KEY - Dialpad API key (required for contact lookup)
- DIALPAD_WEBHOOK_SECRET - webhook auth secret (optional, enables signature/JWT verification)
- OPENCLAW_GATEWAY_URL (default: http://127.0.0.1:8080)
- OPENCLAW_HOOKS_TOKEN (required for OpenClaw hook forwarding)
- OPENCLAW_HOOKS_PATH (default: /hooks/agent)
- OPENCLAW_HOOKS_NAME (default: Dialpad SMS)
- OPENCLAW_HOOKS_CALL_NAME (default: Dialpad Missed Call)
- OPENCLAW_HOOKS_CHANNEL (optional)
- OPENCLAW_HOOKS_TO (optional)
- OPENCLAW_HOOKS_AGENT_ID (optional)
- OPENCLAW_HOOKS_SMS_ENABLED (default: disabled)
- OPENCLAW_HOOKS_CALL_ENABLED (default: disabled)
"""

import json
import os
import sys
import time
import hmac
import hashlib
import base64
import binascii
import re
import sqlite3
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Add skill directory to path for local imports
skill_dir = Path(__file__).parent
sys.path.insert(0, str(skill_dir))

# Import existing SQLite storage handler
from webhook_sqlite import handle_sms_webhook
try:
    from sms_sqlite import init_db as init_sms_history_db
except Exception:
    init_sms_history_db = None
try:
    from send_sms import send_sms as dialpad_send_sms
except Exception:
    dialpad_send_sms = None

try:
    import sms_approval
except Exception:
    sms_approval = None


def parse_bool_env(raw_value, default=True):
    """Parse common truthy/falsey env values, falling back when unset."""
    if raw_value is None:
        return default
    text = str(raw_value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


# Environment configuration (NO HARDCODED SECRETS)
PORT = int(os.environ.get("PORT", "8081"))
WEBHOOK_SECRET = os.environ.get("DIALPAD_WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("DIALPAD_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("DIALPAD_TELEGRAM_CHAT_ID", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
DIALPAD_API_KEY = os.environ.get("DIALPAD_API_KEY", "")
DIALPAD_LINE_NAMES = os.environ.get("DIALPAD_LINE_NAMES", "")
OPENCLAW_GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:8080")
OPENCLAW_HOOKS_TOKEN = os.environ.get("OPENCLAW_HOOKS_TOKEN", "")
OPENCLAW_HOOKS_PATH = os.environ.get("OPENCLAW_HOOKS_PATH", "/hooks/agent")
OPENCLAW_HOOKS_NAME = os.environ.get("OPENCLAW_HOOKS_NAME", "Dialpad SMS")
OPENCLAW_HOOKS_CALL_NAME = os.environ.get("OPENCLAW_HOOKS_CALL_NAME", "Dialpad Missed Call")
OPENCLAW_HOOKS_CHANNEL = os.environ.get("OPENCLAW_HOOKS_CHANNEL", "")
OPENCLAW_HOOKS_TO = os.environ.get("OPENCLAW_HOOKS_TO", "")
OPENCLAW_HOOKS_AGENT_ID = os.environ.get("OPENCLAW_HOOKS_AGENT_ID", "")
OPENCLAW_HOOKS_SMS_ENABLED = parse_bool_env(os.environ.get("OPENCLAW_HOOKS_SMS_ENABLED"), False)
OPENCLAW_HOOKS_CALL_ENABLED = parse_bool_env(os.environ.get("OPENCLAW_HOOKS_CALL_ENABLED"), False)
DIALPAD_SMS_TELEGRAM_NOTIFY = os.environ.get("DIALPAD_SMS_TELEGRAM_NOTIFY", "1").lower() in {"1", "true", "yes", "on"}
DIALPAD_TELEGRAM_APPROVAL_BUTTONS_ENABLED = parse_bool_env(
    os.environ.get("DIALPAD_TELEGRAM_APPROVAL_BUTTONS_ENABLED"),
    False,
)
DIALPAD_PRIORITY_ROUTE_TO = os.environ.get("DIALPAD_PRIORITY_ROUTE_TO", "")
DIALPAD_PRIORITY_ROUTE_PHONES = os.environ.get("DIALPAD_PRIORITY_ROUTE_PHONES", "")
DIALPAD_AUTO_REPLY_ENABLED = parse_bool_env(os.environ.get("DIALPAD_AUTO_REPLY_ENABLED"), False)

DEFAULT_LINE_NAMES = {
    "+14155201316": "Sales",
    "+14153602954": "Work",
    "+14159917155": "Support",
    "+14159065785": "Main",
    "+18332974273": "Main",
}


MISSED_CALL_STATES = {"missed", "no_answer", "unanswered"}
MISSED_CALL_EVENT_HINTS = {"missed_call", "call.missed", "call_missed", "call missed"}
CALL_CONTEXT_FIELDS = {
    "call_id",
    "call_missed",
    "call_state",
    "call_direction",
    "call_duration",
    "duration",
}
MISSED_CALL_DEDUPE_TABLE = "missed_call_webhook_events"
MISSED_CALL_DEDUPE_FALLBACK_BUCKET_MS = 60 * 1000
MISSED_CALL_DEDUPE_RETENTION_MS = 7 * 24 * 60 * 60 * 1000

TELEGRAM_STATUS_SENT = "sent"
TELEGRAM_STATUS_FILTERED = "filtered"
TELEGRAM_STATUS_NOT_APPLICABLE = "not_applicable"
TELEGRAM_STATUS_FAILED = "failed"

TELEGRAM_CALLBACK_NAMESPACE = "smsa"
TELEGRAM_CALLBACK_APPROVE = "a"
TELEGRAM_CALLBACK_REJECT = "r"
TELEGRAM_CALLBACK_CONFIRM_RISK = "c"
TELEGRAM_CALLBACK_MAX_BYTES = 64
TELEGRAM_TERMINAL_APPROVAL_STATUSES = {
    "already_resolved",
    "blocked_opt_out",
    "failed",
    "not_found",
    "rejected",
    "sent",
    "stale",
}

SENSITIVE_KEYWORD_PATTERNS = (
    re.compile(
        r"\b("
        r"otp|o\.t\.p|"
        r"2fa|two[- ]?factor|multi[- ]?factor|mfa|"
        r"verification code|security code|auth(?:entication)? code|"
        r"one[- ]?time (?:pass(?:word)?|code)|passcode"
        r")\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:google|g-?code|intuit|bank|chase|wells fargo|bank of america|"
        r"citi|capital one|paypal|venmo)\b.{0,80}\b(?:code|otp|passcode|verification)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:code|otp|passcode|verification code)\b.{0,30}\b\d{4,8}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d{4,8}\b.{0,30}\b(?:code|otp|passcode|verification code)\b",
        re.IGNORECASE,
    ),
)

CODE_TOKEN_PATTERN = re.compile(r"\b(?:\d[\s-]?){4,8}\b")
CALLS_ENDPOINT = "https://dialpad.com/api/v2/call"

OPT_OUT_PATTERNS = (
    re.compile(r"^\s*(stop|stopall|unsubscribe|cancel|end|quit)\s*[.!]?\s*$", re.IGNORECASE),
    re.compile(r"\bstop\s+(texting|messaging|calling|contacting|reaching out|sending)\b", re.IGNORECASE),
    re.compile(r"\b(unsubscribe|remove me|do not contact|don't contact)\b", re.IGNORECASE),
    re.compile(r"\b(do not|don't|please don't)\s+bother me\b", re.IGNORECASE),
    re.compile(r"\bleave me alone\b", re.IGNORECASE),
)

RISKY_REPLY_PATTERNS = (
    re.compile(r"\b(real person|human|representative|manager)\b", re.IGNORECASE),
    re.compile(r"\b(lawyer|attorney|legal|complaint|report you)\b", re.IGNORECASE),
    re.compile(r"\b(confused|confusion|wrong time|already|thought today|when are we)\b", re.IGNORECASE),
    re.compile(r"\b(angry|upset|frustrated|annoyed)\b", re.IGNORECASE),
)


def log_line(message):
    """Emit unbuffered logs so systemd journal always shows webhook hits."""
    print(message, flush=True)


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


DIALPAD_AUTO_REPLY_SALES_LINE = normalize_phone_number(
    os.environ.get("DIALPAD_AUTO_REPLY_SALES_LINE", "+14155201316")
)
INBOUND_CONTEXT_FRESHNESS_WINDOW_MS = 14 * 24 * 60 * 60 * 1000
CURRENT_EVENT_EXCLUSION_MS = 60 * 1000


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
    except Exception as e:
        print(f"⚠️  Invalid DIALPAD_LINE_NAMES, using defaults: {e}")

    return loaded


LINE_NAMES = load_line_names()


def parse_priority_route_phones(raw_value):
    """Parse comma-separated E.164 phone list into normalized set."""
    values = set()
    for part in (raw_value or "").split(","):
        normalized = normalize_phone_number(part.strip())
        if normalized:
            values.add(normalized)
    return values


PRIORITY_ROUTE_PHONES = parse_priority_route_phones(DIALPAD_PRIORITY_ROUTE_PHONES)


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


def infer_line_display_from_payload(data):
    """Best-effort line detection from missed-call payload when to_number is absent."""
    direct = extract_number(
        data,
        "to_number",
        "called_number",
        "line_number",
        "mainline_number",
        "phone_number",
        "target_number",
        "to",
    )
    if direct:
        resolved = get_line_name(direct)
        if resolved:
            return resolved

    try:
        blob = json.dumps(data, separators=(",", ":"))
    except Exception:
        return None

    for normalized in sorted(LINE_NAMES.keys(), key=len, reverse=True):
        if normalized and normalized in blob:
            return get_line_name(normalized)

    return None


def get_contact_name(phone_number):
    """Compatibility helper that returns contact name only."""
    return lookup_contact_enrichment(phone_number).get("contact_name")


def _flatten_strings(value, out):
    """Collect nested string values from decoded JSON-like structures."""
    if isinstance(value, str):
        out.append(value)
        return
    if isinstance(value, dict):
        for nested in value.values():
            _flatten_strings(nested, out)
        return
    if isinstance(value, list):
        for nested in value:
            _flatten_strings(nested, out)


def contact_contains_phone(contact, phone_number):
    """Return True only when a Dialpad contact payload includes the queried phone."""
    expected = normalize_phone_number(phone_number)
    if not expected or not isinstance(contact, dict):
        return False

    values = []
    for key in ("phones", "phone_numbers", "primary_phone", "phone", "number", "numbers"):
        _flatten_strings(contact.get(key), values)

    for value in values:
        if normalize_phone_number(value) == expected:
            return True
    return False


def format_contact_enrichment(contact):
    """Build sender enrichment fields from a verified Dialpad contact."""
    first_name = str(contact.get("first_name", "") or "").strip()
    last_name = str(contact.get("last_name", "") or "").strip()
    company = str(contact.get("company", "") or "").strip()
    title = str(contact.get("job_title", "") or "").strip()
    name = f"{first_name} {last_name}".strip()
    info = name or "Known Contact"
    if company:
        info += f" ({company})"
    if title:
        info = f"{title} | {info}"
    return {
        "contact_name": info,
        "first_name": first_name or None,
        "last_name": last_name or None,
        "company": company or None,
        "job_title": title or None,
        "status": "resolved",
        "degraded": False,
        "degraded_reason": None,
    }


def _extract_unauthorized_hint_text(raw_body):
    """Decode 401 body into searchable hint text (never logged directly)."""
    if not raw_body:
        return ""
    text = raw_body.decode("utf-8", errors="ignore")
    try:
        decoded = json.loads(text)
    except Exception:
        return text.lower()

    values = []
    _flatten_strings(decoded, values)
    return " ".join(values).lower()


def classify_contact_lookup_unauthorized(raw_body):
    """
    Classify 401 contact-lookup failures without exposing sensitive payloads.
    Returns one of: expired_token, missing_scope, invalid_audience_or_environment, unauthorized.
    """
    hints = _extract_unauthorized_hint_text(raw_body)
    if not hints:
        return "unauthorized"

    if any(token in hints for token in ("expired", "expiration", "token_expired", "jwt expired")):
        return "expired_token"

    if any(
        token in hints
        for token in (
            "scope",
            "insufficient permission",
            "insufficient_permissions",
            "permission denied",
            "not authorized to access",
        )
    ):
        return "missing_scope"

    if any(
        token in hints
        for token in (
            "audience",
            "invalid audience",
            "issuer",
            "invalid issuer",
            "wrong environment",
            "environment mismatch",
            "sandbox",
            "production",
        )
    ):
        return "invalid_audience_or_environment"

    return "unauthorized"


def lookup_contact_enrichment(phone_number):
    """
    Resolve sender enrichment from Dialpad contacts endpoint.
    Returns a dict with contact_name and explicit degraded-enrichment details.
    """
    result = {
        "contact_name": None,
        "first_name": None,
        "last_name": None,
        "company": None,
        "job_title": None,
        "status": "disabled",
        "degraded": False,
        "degraded_reason": None,
    }
    if not DIALPAD_API_KEY:
        return result

    phone_value = str(phone_number or "").strip()
    if not phone_value or phone_value.upper() == "N/A":
        result["status"] = "not_found"
        return result

    result["status"] = "not_found"
    search_url = f"https://dialpad.com/api/v2/contacts?query={urllib.parse.quote(phone_value)}"
    headers = {
        "Authorization": f"Bearer {DIALPAD_API_KEY}",
        "Accept": "application/json",
    }

    try:
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            items = data.get("items", [])
            for contact in items:
                if contact_contains_phone(contact, phone_value):
                    result.update(format_contact_enrichment(contact))
                    break
            return result
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raw_body = b""
            try:
                raw_body = e.read() or b""
            except Exception:
                raw_body = b""
            reason = classify_contact_lookup_unauthorized(raw_body)
            result["status"] = "unauthorized"
            result["degraded"] = True
            result["degraded_reason"] = reason
            print(f"⚠️  Dialpad contact lookup unauthorized ({reason})")
            return result
        result["status"] = f"http_{e.code}"
        result["degraded"] = True
        result["degraded_reason"] = "lookup_http_error"
        print(f"⚠️  Dialpad contact lookup failed (http_{e.code})")
        return result
    except Exception as e:
        result["status"] = "request_failed"
        result["degraded"] = True
        result["degraded_reason"] = "lookup_request_failed"
        print(f"⚠️  Dialpad contact lookup failed ({type(e).__name__})")
        return result


def extract_message_text(data):
    """Extract text payload from webhook event as a string."""
    text = data.get("text", "")
    text_content = data.get("text_content", "")

    if not is_blank_text(text):
        return str(text)
    if not is_blank_text(text_content):
        return str(text_content)
    return str(text or text_content or "")


def is_blank_text(value):
    """True when text is empty or whitespace-only."""
    return not str(value or "").strip()


def is_short_code_sender(phone_number):
    """Return True for likely short-code senders (4-6 digits)."""
    digits = "".join(ch for ch in str(phone_number or "") if ch.isdigit())
    return 4 <= len(digits) <= 6


def is_sensitive_message(text="", sender="", contact_number=""):
    """
    Return True for OTP/2FA/security verification messages.
    These messages are stored, but must not be forwarded to Telegram.
    """
    body = str(text or "")
    if not body.strip():
        return False

    combined = " ".join(
        part for part in (str(sender or ""), str(contact_number or ""), body) if part
    )

    for pattern in SENSITIVE_KEYWORD_PATTERNS:
        if pattern.search(combined):
            return True

    has_code = bool(CODE_TOKEN_PATTERN.search(body))
    has_security_context = bool(
        re.search(
            r"\b(verify|verification|security|login|signin|sign in|auth|account|bank|google|intuit)\b",
            combined,
            re.IGNORECASE,
        )
    )
    return has_code and has_security_context


def classify_sms_reply_policy(text):
    """Classify inbound text for deterministic SMS reply safety."""
    body = str(text or "")
    for pattern in OPT_OUT_PATTERNS:
        if pattern.search(body):
            return {
                "state": "blocked_opt_out",
                "reason_code": "filtered_opt_out",
                "risk_reason": "explicit opt-out language",
            }
    for pattern in RISKY_REPLY_PATTERNS:
        if pattern.search(body):
            return {
                "state": "risky",
                "reason_code": "risky_confirmation_required",
                "risk_reason": f"matched risky phrase: {pattern.pattern}",
            }
    return {
        "state": "normal",
        "reason_code": "eligible",
        "risk_reason": None,
    }


def first_value(value):
    """Return first item for list-like values, otherwise passthrough."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def extract_number(data, *keys):
    """Extract first non-empty phone-like value from top-level and nested payload objects."""
    if not isinstance(data, dict):
        return None

    for key in keys:
        val = first_value(data.get(key))
        if isinstance(val, str) and val.strip():
            return val.strip()

    for nested_key in ("call", "event", "data", "payload"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for key in keys:
                val = first_value(nested.get(key))
                if isinstance(val, str) and val.strip():
                    return val.strip()

    return None


def detect_reliable_missed_call_hint(data):
    """
    Detect missed-call events routed through the SMS webhook path.
    Conservative by design: requires blank text plus explicit missed-call signal.
    """
    if not isinstance(data, dict):
        return False

    if str(data.get("direction", "")).lower() != "inbound":
        return False

    if not is_blank_text(extract_message_text(data)):
        return False

    event_fields = ("event_type", "event", "type", "subscription_type", "topic")
    event_text = " ".join(str(data.get(k, "")).lower() for k in event_fields)
    call_state = str(data.get("call_state", "")).lower()

    has_missed_signal = (
        data.get("call_missed") is True
        or data.get("missed_call") is True
        or data.get("is_missed_call") is True
        or call_state in MISSED_CALL_STATES
        or any(hint in event_text for hint in MISSED_CALL_EVENT_HINTS)
        or ("call" in event_text and ("no_answer" in event_text or "unanswered" in event_text))
    )
    if not has_missed_signal:
        return False

    has_call_context = any(key in data for key in CALL_CONTEXT_FIELDS) or "call" in event_text
    if not has_call_context:
        return False

    from_num = extract_number(data, "from_number", "caller_number", "from")
    return bool(str(from_num or "").strip())


def classify_inbound_notification(data):
    """
    Classify inbound webhook payload for Telegram behavior.
    Returns one of: sms, missed_call, blank_sms, not_inbound.
    """
    if str(data.get("direction", "")).lower() != "inbound":
        return "not_inbound"
    if detect_reliable_missed_call_hint(data):
        return "missed_call"
    if is_blank_text(extract_message_text(data)):
        return "blank_sms"
    return "sms"


def assess_inbound_sms_alert_eligibility(
    data,
    *,
    from_number,
    text,
    sender="",
    notification_type=None,
):
    """
    Centralized eligibility decision for inbound SMS alert fan-out.
    Returns a reason code safe for logs/response metadata.
    """
    resolved_type = notification_type or classify_inbound_notification(data)
    if resolved_type == "missed_call":
        return {
            "eligible": False,
            "reason_code": "filtered_missed_call",
            "sensitive_filtered": False,
            "notification_type": resolved_type,
        }
    if resolved_type == "blank_sms":
        return {
            "eligible": False,
            "reason_code": "filtered_blank_sms",
            "sensitive_filtered": False,
            "notification_type": resolved_type,
        }
    if resolved_type != "sms":
        return {
            "eligible": False,
            "reason_code": "not_inbound",
            "sensitive_filtered": False,
            "notification_type": resolved_type,
        }
    if is_short_code_sender(from_number):
        return {
            "eligible": False,
            "reason_code": "filtered_shortcode",
            "sensitive_filtered": True,
            "notification_type": resolved_type,
        }
    reply_policy = classify_sms_reply_policy(text)
    if reply_policy["state"] == "blocked_opt_out":
        return {
            "eligible": False,
            "reason_code": reply_policy["reason_code"],
            "sensitive_filtered": False,
            "notification_type": resolved_type,
            "reply_policy": reply_policy,
        }
    if is_sensitive_message(text=text, sender=sender, contact_number=from_number):
        return {
            "eligible": False,
            "reason_code": "filtered_sensitive",
            "sensitive_filtered": True,
            "notification_type": resolved_type,
            "reply_policy": reply_policy,
        }
    return {
        "eligible": True,
        "reason_code": "eligible",
        "sensitive_filtered": False,
        "notification_type": resolved_type,
        "reply_policy": reply_policy,
    }


def escape_telegram_markdown(text):
    """Escape Telegram MarkdownV1 control characters in dynamic content."""
    if text is None:
        return ""
    escaped = str(text)
    for ch in ("_", "*", "`", "["):
        escaped = escaped.replace(ch, f"\\{ch}")
    return escaped


def telegram_buttons_available():
    """Return True when inline approval buttons can be safely rendered."""
    return bool(
        DIALPAD_TELEGRAM_APPROVAL_BUTTONS_ENABLED
        and TELEGRAM_BOT_TOKEN
        and TELEGRAM_CHAT_ID
        and TELEGRAM_WEBHOOK_SECRET
    )


def build_telegram_callback_data(action, draft_id):
    """Build compact Telegram callback data and enforce Bot API length limits."""
    if not action or not draft_id:
        return None
    callback_data = f"{TELEGRAM_CALLBACK_NAMESPACE}:{action}:{draft_id}"
    if len(callback_data.encode("utf-8")) > TELEGRAM_CALLBACK_MAX_BYTES:
        return None
    return callback_data


def parse_telegram_callback_data(callback_data):
    """Parse SMS approval callback data into an action and draft id."""
    parts = str(callback_data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != TELEGRAM_CALLBACK_NAMESPACE:
        return None
    action, draft_id = parts[1], parts[2]
    if action not in {
        TELEGRAM_CALLBACK_APPROVE,
        TELEGRAM_CALLBACK_REJECT,
        TELEGRAM_CALLBACK_CONFIRM_RISK,
    }:
        return None
    if not draft_id.startswith("smsdraft_"):
        return None
    return {"action": action, "draft_id": draft_id}


def build_sms_approval_reply_markup(draft_id, reply_policy=None, *, risk_confirmation=False):
    """Build Telegram InlineKeyboardMarkup for an SMS approval draft."""
    if not telegram_buttons_available() or not draft_id:
        return None

    approve_action = TELEGRAM_CALLBACK_CONFIRM_RISK if risk_confirmation else TELEGRAM_CALLBACK_APPROVE
    approve_data = build_telegram_callback_data(approve_action, draft_id)
    reject_data = build_telegram_callback_data(TELEGRAM_CALLBACK_REJECT, draft_id)
    if not approve_data or not reject_data:
        return None

    reply_policy = reply_policy or {}
    risk_state = reply_policy.get("state")
    if risk_confirmation:
        approve_label = "Confirm send"
    elif risk_state == "risky":
        approve_label = "Acknowledge risk"
    else:
        approve_label = "Approve send"

    return {
        "inline_keyboard": [
            [
                {"text": approve_label, "callback_data": approve_data},
                {"text": "Reject", "callback_data": reject_data},
            ]
        ]
    }


def call_telegram_api(method, payload, *, timeout=10):
    """Call Telegram Bot API and return True on success."""
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️  Telegram not configured (missing BOT_TOKEN)")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception as e:
        print(f"❌ Error calling Telegram {method}: {e}")
        return False


def send_to_telegram(text, reply_markup=None):
    """
    Send a message to the configured Telegram channel.
    Returns True on success, False on failure (non-blocking).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram not configured (missing BOT_TOKEN or CHAT_ID)")
        return False

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    return call_telegram_api("sendMessage", payload)


def answer_telegram_callback(callback_query_id, text=None, *, show_alert=False):
    """Stop Telegram's callback spinner and optionally show operator feedback."""
    if not callback_query_id:
        return False
    payload = {
        "callback_query_id": callback_query_id,
        "show_alert": show_alert,
    }
    if text:
        payload["text"] = str(text)[:200]
    return call_telegram_api("answerCallbackQuery", payload, timeout=5)


def edit_telegram_message_reply_markup(chat_id, message_id, reply_markup=None):
    """Replace or remove a Telegram message inline keyboard."""
    if chat_id is None or message_id is None:
        return False
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return call_telegram_api("editMessageReplyMarkup", payload, timeout=5)


def edit_telegram_message_text(chat_id, message_id, text, reply_markup=None):
    """Edit Telegram review message text and optional inline keyboard."""
    if chat_id is None or message_id is None:
        return False
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return call_telegram_api("editMessageText", payload, timeout=5)


def build_telegram_callback_status_message(result, draft_id):
    """Build visible group status for a Telegram approval callback result."""
    result = result or {}
    status = result.get("status") or "unknown"
    draft = result.get("draft") or {}
    resolved_draft_id = draft.get("draft_id") or draft_id or "unknown"
    escaped_draft_id = escape_telegram_markdown(resolved_draft_id)

    if result.get("sent"):
        sms_id = result.get("dialpad_sms_id") or draft.get("dialpad_sms_id") or "unknown"
        return (
            "✅ *SMS approval sent*\n"
            f"*Draft ID:* `{escaped_draft_id}`\n"
            f"*Dialpad SMS ID:* `{escape_telegram_markdown(sms_id)}`"
        )
    if status == "risky_confirmation_required":
        reason = result.get("risk_reason") or draft.get("risk_reason") or "risk policy matched"
        return (
            "⚠️ *Risk acknowledged \\(not sent\\)*\n"
            f"*Draft ID:* `{escaped_draft_id}`\n"
            f"*Risk:* {escape_telegram_markdown(reason)}\n"
            "Tap *Confirm send* to send this exact draft."
        )
    if status == "rejected":
        return (
            "🚫 *SMS approval rejected \\(not sent\\)*\n"
            f"*Draft ID:* `{escaped_draft_id}`"
        )
    if status == "already_resolved":
        return (
            "ℹ️ *SMS approval already resolved*\n"
            f"*Draft ID:* `{escaped_draft_id}`"
        )

    reason = result.get("reason") or result.get("error") or status
    return (
        "⛔ *SMS approval not sent*\n"
        f"*Draft ID:* `{escaped_draft_id}`\n"
        f"*Reason:* {escape_telegram_markdown(reason)}"
    )


def callback_answer_text(result):
    """Build short Telegram toast text for callback query responses."""
    result = result or {}
    if result.get("sent"):
        return "Sent."
    status = result.get("status")
    if status == "risky_confirmation_required":
        return "Risk acknowledged. Confirm send required."
    if status == "rejected":
        return "Rejected. Not sent."
    if status == "already_resolved":
        return "Already resolved."
    reason = result.get("reason") or result.get("error") or status or "Not sent."
    return f"Not sent: {reason}"


def update_telegram_review_after_callback(callback_query, result, draft_id):
    """Update Telegram controls and post a visible status after a callback."""
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    status = (result or {}).get("status")

    if status == "risky_confirmation_required":
        reply_markup = build_sms_approval_reply_markup(
            draft_id,
            {"state": "risky"},
            risk_confirmation=True,
        )
        edit_telegram_message_reply_markup(chat_id, message_id, reply_markup=reply_markup)
    elif (result or {}).get("sent") or status in TELEGRAM_TERMINAL_APPROVAL_STATUSES:
        edit_telegram_message_reply_markup(chat_id, message_id, reply_markup=None)

    return send_to_telegram(build_telegram_callback_status_message(result, draft_id))


def dispatch_telegram_approval_callback(callback_query, parsed_callback):
    """Execute an authenticated Telegram approval callback against the approval ledger."""
    if sms_approval is None:
        return {
            "ok": False,
            "status": "approval_unavailable",
            "sent": False,
            "reason": "approval module unavailable",
        }

    actor = callback_query.get("from") or {}
    actor_id = str(actor.get("id") or "")
    actor_username = actor.get("username")
    actor_is_bot = bool(actor.get("is_bot"))
    draft_id = parsed_callback["draft_id"]
    action = parsed_callback["action"]

    try:
        conn = sms_approval.init_db()
        try:
            if action == TELEGRAM_CALLBACK_REJECT:
                return sms_approval.reject_draft(
                    conn,
                    draft_id=draft_id,
                    actor_id=actor_id,
                    actor_username=actor_username,
                    actor_is_bot=actor_is_bot,
                )
            approval_action = (
                sms_approval.ACTION_CONFIRM_RISK
                if action == TELEGRAM_CALLBACK_CONFIRM_RISK
                else sms_approval.ACTION_APPROVE
            )
            return sms_approval.approve_draft(
                conn,
                draft_id=draft_id,
                actor_id=actor_id,
                actor_username=actor_username,
                actor_is_bot=actor_is_bot,
                action=approval_action,
                send_func=dialpad_send_sms,
            )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - callbacks must fail closed.
        print(f"⚠️  Telegram approval callback failed ({type(exc).__name__})")
        return {
            "ok": False,
            "status": "callback_failed",
            "sent": False,
            "reason": str(exc),
            "draft": {"draft_id": draft_id},
        }


def _get_header(headers, name):
    """Fetch header by name with case-insensitive fallback."""
    value = headers.get(name) if hasattr(headers, "get") else None
    if value:
        return value

    lowered = name.lower()
    if isinstance(headers, dict):
        for key, header_value in headers.items():
            if str(key).lower() == lowered:
                return header_value
    return None


def parse_signature_candidates(header_value):
    """
    Parse signature header into hex digest candidates.
    Supports raw hex and prefixed forms like sha256=<hex>.
    """
    if not header_value:
        return []

    candidates = []
    for part in str(header_value).split(","):
        piece = part.strip()
        if not piece:
            continue
        if "=" in piece:
            piece = piece.split("=", 1)[1].strip()
        elif ":" in piece:
            piece = piece.split(":", 1)[1].strip()
        piece = piece.lower()
        if len(piece) == 64 and all(ch in "0123456789abcdef" for ch in piece):
            candidates.append(piece)
    return candidates


def verify_hmac_signature(raw_body, headers, secret):
    """Verify Dialpad HMAC SHA256 signature header against raw request body."""
    if not secret:
        return True

    sig_values = [
        _get_header(headers, "X-Dialpad-Signature"),
        _get_header(headers, "X-Dialpad-Signature-SHA256"),
    ]
    provided = []
    for sig_value in sig_values:
        provided.extend(parse_signature_candidates(sig_value))

    if not provided:
        return False

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(candidate, expected) for candidate in provided)


def _b64url_decode(segment):
    """Decode a base64url segment with optional omitted padding."""
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def extract_bearer_token(headers):
    """Extract bearer token from Authorization header."""
    auth = _get_header(headers, "Authorization")
    if not auth:
        return None
    parts = auth.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def verify_bearer_jwt(headers, secret):
    """
    Verify Authorization: Bearer <jwt> token using HS256 secret.
    Signature validation only (best-effort stdlib verification).
    """
    if not secret:
        return True

    token = extract_bearer_token(headers)
    if not token:
        return False

    parts = token.split(".")
    if len(parts) != 3:
        return False

    header_b64, payload_b64, signature_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")

    try:
        header_obj = json.loads(_b64url_decode(header_b64).decode("utf-8"))
        signature_bytes = _b64url_decode(signature_b64)
    except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
        return False

    if header_obj.get("alg") != "HS256":
        return False

    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return hmac.compare_digest(signature_bytes, expected)


def verify_webhook_auth(headers, raw_body, secret):
    """
    Validate inbound webhook auth when a secret is configured.
    Accepts either Dialpad HMAC signature headers or Bearer HS256 JWT.
    """
    if not secret:
        return True, "disabled"
    if verify_hmac_signature(raw_body, headers, secret):
        return True, "hmac"
    if verify_bearer_jwt(headers, secret):
        return True, "jwt"
    return False, "missing_or_invalid_signature_or_jwt"


def _first_value(value):
    """Return first item for list-like values, otherwise return value unchanged."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _clean_str(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _get_nested(data, path):
    current = data
    for key in path:
        if isinstance(current, list):
            current = current[0] if current else None
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, list):
        current = current[0] if current else None
    return current


def _pick_nested(data, paths):
    for path in paths:
        value = _clean_str(_get_nested(data, path))
        if value:
            return value
    return None


def _parse_timestamp_ms(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            numeric = float(text)
        except ValueError:
            iso = text.replace("Z", "+00:00")
            try:
                return int(datetime.fromisoformat(iso).timestamp() * 1000)
            except ValueError:
                return None
    if numeric > 10_000_000_000:
        return int(numeric)
    return int(numeric * 1000)


def _now_ms():
    return int(time.time() * 1000)


def _clean_identifier(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_missed_call_dedupe_key(data, resolved_context):
    """Build a stable idempotency key for Dialpad missed-call parent/child events."""
    data = data or {}
    resolved_context = resolved_context or {}
    root_call_id = _clean_identifier(data.get("entry_point_call_id"))
    if root_call_id:
        return f"missed-call:root:{root_call_id}"

    call_id = _clean_identifier(data.get("call_id") or data.get("id"))
    if call_id:
        return f"missed-call:root:{call_id}"

    sender_number = normalize_phone_number(resolved_context.get("from_number"))
    recipient_number = normalize_phone_number(resolved_context.get("to_number"))
    event_ts_ms = _parse_timestamp_ms(resolved_context.get("event_ts_ms"))
    if event_ts_ms is None:
        event_ts_ms = _parse_timestamp_ms(
            _pick_nested(
                data,
                [
                    ("date_started",),
                    ("date_start",),
                    ("start_time",),
                    ("timestamp",),
                    ("event_timestamp",),
                    ("event", "timestamp"),
                    ("call", "date_started"),
                ],
            )
        )
    bucket = event_ts_ms // MISSED_CALL_DEDUPE_FALLBACK_BUCKET_MS if event_ts_ms is not None else "unknown"
    return f"missed-call:fingerprint:{sender_number or 'unknown'}:{recipient_number or 'unknown'}:{bucket}"


def _missed_call_dedupe_db_path():
    if sms_approval is not None and getattr(sms_approval, "DB_PATH", None):
        return Path(sms_approval.DB_PATH)
    return Path(os.environ.get("DIALPAD_MISSED_CALL_DEDUPE_DB", "/home/art/clawd/logs/sms_approvals.db"))


def _init_missed_call_dedupe_db(db_path=None):
    path = Path(db_path) if db_path is not None else _missed_call_dedupe_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MISSED_CALL_DEDUPE_TABLE} (
            dedupe_key TEXT PRIMARY KEY,
            first_seen_at_ms INTEGER NOT NULL,
            last_seen_at_ms INTEGER NOT NULL,
            duplicate_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    return conn


def claim_missed_call_notification(dedupe_key, *, db_path=None, now_ms=None):
    """Atomically claim a missed-call notification key, failing open if storage is unavailable."""
    key = _clean_identifier(dedupe_key)
    if not key:
        return {"claimed": True, "duplicate": False, "key": None, "status": "key_missing"}

    timestamp_ms = _now_ms() if now_ms is None else now_ms
    try:
        conn = _init_missed_call_dedupe_db(db_path=db_path)
        try:
            conn.execute(
                f"DELETE FROM {MISSED_CALL_DEDUPE_TABLE} WHERE first_seen_at_ms < ?",
                (timestamp_ms - MISSED_CALL_DEDUPE_RETENTION_MS,),
            )
            cursor = conn.execute(
                f"""
                INSERT OR IGNORE INTO {MISSED_CALL_DEDUPE_TABLE}
                    (dedupe_key, first_seen_at_ms, last_seen_at_ms, duplicate_count)
                VALUES (?, ?, ?, 0)
                """,
                (key, timestamp_ms, timestamp_ms),
            )
            if cursor.rowcount == 1:
                conn.commit()
                return {"claimed": True, "duplicate": False, "key": key, "status": "claimed"}
            conn.execute(
                f"""
                UPDATE {MISSED_CALL_DEDUPE_TABLE}
                SET last_seen_at_ms = ?, duplicate_count = duplicate_count + 1
                WHERE dedupe_key = ?
                """,
                (timestamp_ms, key),
            )
            conn.commit()
            return {"claimed": False, "duplicate": True, "key": key, "status": "duplicate"}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - webhook notifications should fail open.
        print(f"⚠️  Missed-call dedupe unavailable ({type(exc).__name__})")
        return {"claimed": True, "duplicate": False, "key": key, "status": "dedupe_unavailable"}


def _extract_payload_contact_name(data):
    """Extract a Dialpad-provided contact display name from webhook payloads."""
    contact = data.get("contact")
    if isinstance(contact, dict):
        for key in ("name", "display_name", "full_name"):
            value = contact.get(key)
            if value and str(value).strip() and str(value).strip() != str(data.get("from_number", "")).strip():
                return str(value).strip()

    for path in (
        ("customer", "name"),
        ("caller", "name"),
        ("from", "name"),
        ("call", "contact", "name"),
        ("event", "contact", "name"),
    ):
        value = _pick_nested(data, [path])
        if value and str(value).strip():
            return str(value).strip()
    return None


def apply_payload_contact_fallback(sender_enrichment, data):
    """Use webhook contact identity as display-only context when exact lookup has no name."""
    sender_enrichment = dict(sender_enrichment or {})
    if sender_enrichment.get("contact_name"):
        return sender_enrichment

    payload_name = _extract_payload_contact_name(data)
    if not payload_name:
        return sender_enrichment

    sender_enrichment["contact_name"] = payload_name
    sender_enrichment.setdefault("first_name", payload_name.split()[0] if payload_name else None)
    sender_enrichment.setdefault("last_name", None)
    sender_enrichment.setdefault("company", None)
    sender_enrichment.setdefault("job_title", None)
    if sender_enrichment.get("status") in {None, "not_found", "not_applicable", "disabled"}:
        sender_enrichment["status"] = "payload_contact"
    sender_enrichment["payload_contact_name"] = payload_name
    sender_enrichment["payload_contact_used"] = True
    return sender_enrichment


def _context_age_days(event_ts_ms, last_activity_ms):
    if event_ts_ms is None or last_activity_ms is None:
        return None
    delta_ms = max(0, event_ts_ms - last_activity_ms)
    return round(delta_ms / (24 * 60 * 60 * 1000), 1)


def lookup_recent_sms_context(customer_number, *, current_dialpad_id=None, current_timestamp_ms=None):
    """Return the latest prior SMS activity for a customer, if local history exists."""
    if init_sms_history_db is None or not customer_number:
        return None
    if current_dialpad_id is None and current_timestamp_ms is None:
        return None

    clauses = ["contact_number = ?"]
    params = [customer_number]
    if current_dialpad_id is not None:
        clauses.append("(dialpad_id IS NULL OR dialpad_id != ?)")
        params.append(current_dialpad_id)
    if current_timestamp_ms is not None:
        clauses.append("(timestamp IS NULL OR timestamp < ?)")
        params.append(current_timestamp_ms)

    query = (
        "SELECT direction, timestamp, text FROM messages "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY timestamp DESC, id DESC LIMIT 1"
    )
    try:
        conn = init_sms_history_db()
        try:
            row = conn.execute(query, params).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - context lookup should never break webhooks.
        print(f"⚠️  Recent SMS context lookup failed ({type(exc).__name__})")
        return None

    if not row:
        return None

    return {
        "source": "local_sms_history",
        "lastActivityAt": row["timestamp"],
        "direction": row["direction"],
        "previewAvailable": bool(row["text"]),
    }


def lookup_recent_call_context(customer_number, line_number=None, event_ts_ms=None, history_fetcher=None):
    """Return prior Dialpad call continuity inside the freshness window."""
    normalized_customer = normalize_phone_number(customer_number)
    if not normalized_customer or event_ts_ms is None:
        return None

    fetcher = history_fetcher or _fetch_recent_calls_around
    try:
        history_rows = fetcher(
            event_ts_ms,
            window_ms=INBOUND_CONTEXT_FRESHNESS_WINDOW_MS,
            limit=100,
        )
    except TypeError:
        history_rows = fetcher(event_ts_ms)
    normalized_line = normalize_phone_number(line_number)
    best = None
    best_ts = None
    for call in history_rows:
        row = _extract_call_history_row(call)
        row_ts = row.get("started_ms")
        if row_ts is None:
            continue
        if abs(event_ts_ms - row_ts) <= CURRENT_EVENT_EXCLUSION_MS:
            continue
        if row_ts > event_ts_ms:
            continue

        row_from = normalize_phone_number(row.get("from_number"))
        row_to = normalize_phone_number(row.get("to_number"))
        customer_match = normalized_customer in {row_from, row_to}
        line_match = not normalized_line or normalized_line in {row_from, row_to}
        if not customer_match or not line_match:
            continue
        if best is None or row_ts > best_ts:
            best = row
            best_ts = row_ts

    if not best:
        return None

    return {
        "source": "dialpad_call_history",
        "lastActivityAt": best_ts,
        "direction": best.get("direction"),
        "state": best.get("state"),
    }


def _generic_fallback_draft_allowed(normalized_event, first_contact):
    if not first_contact or first_contact.get("knownContact"):
        return False
    if not first_contact.get("needsDraftReply"):
        return False

    lookup = first_contact.get("lookup") or {}
    if lookup.get("degraded"):
        return False

    lookup_status = str(lookup.get("status") or "")
    if lookup_status in {"disabled", "not_applicable", "resolved"}:
        return False
    if lookup_status == "not_found":
        return True

    return (normalized_event.get("event_type") or "sms") == "sms"


def build_inbound_context(normalized_event, sender_enrichment=None, line_display=None, recent_context=None):
    """Build operator-facing identity, provenance, and draft-safety context."""
    sender_enrichment = sender_enrichment or {}
    first_contact = normalized_event.get("first_contact") or build_first_contact_context(
        normalized_event,
        sender_enrichment=sender_enrichment,
        line_display=line_display,
    )
    event_ts_ms = _parse_timestamp_ms(normalized_event.get("timestamp"))
    lookup = first_contact.get("lookup") if isinstance(first_contact, dict) else {}
    known_contact = bool(first_contact and first_contact.get("knownContact"))
    contact_name = first_contact.get("contactName") if isinstance(first_contact, dict) else None
    degraded = bool((lookup or {}).get("degraded") or sender_enrichment.get("degraded"))
    identity_state = first_contact.get("identityState") if isinstance(first_contact, dict) else "degraded"
    payload_contact_only = bool(sender_enrichment.get("payload_contact_used")) and identity_state != "resolved"

    evidence = []
    if contact_name:
        evidence.append("webhook_contact_payload" if payload_contact_only else "dialpad_contact_name")
    if known_contact and not degraded and not payload_contact_only:
        evidence.append("exact_phone_match")
    if degraded:
        evidence.append("lookup_degraded")
    if not known_contact and identity_state == "not_found":
        evidence.append("no_dialpad_contact_found")

    recency = {
        "state": "unknown",
        "source": None,
        "lastActivityAt": None,
        "ageDays": None,
    }
    if recent_context and recent_context.get("lastActivityAt") is not None:
        last_activity_ms = _parse_timestamp_ms(recent_context.get("lastActivityAt"))
        age_ms = event_ts_ms - last_activity_ms if event_ts_ms is not None and last_activity_ms is not None else None
        age_days = _context_age_days(event_ts_ms, last_activity_ms)
        is_fresh = age_ms is not None and 0 <= age_ms <= INBOUND_CONTEXT_FRESHNESS_WINDOW_MS
        recency = {
            "state": "fresh" if is_fresh else "stale",
            "source": recent_context.get("source"),
            "lastActivityAt": last_activity_ms,
            "ageDays": age_days,
        }
        if recent_context.get("source"):
            evidence.append(recent_context["source"])
    elif known_contact:
        recency["state"] = "unknown"
    else:
        recency["state"] = "not_applicable"

    identity_confidence = "low"
    if known_contact and not degraded and not payload_contact_only:
        identity_confidence = "high"
    elif known_contact:
        identity_confidence = "medium"

    context_draft_allowed = (
        known_contact
        and identity_confidence == "high"
        and recency["state"] == "fresh"
    )
    generic_draft_allowed = _generic_fallback_draft_allowed(normalized_event, first_contact)

    return {
        "identityState": identity_state,
        "identityConfidence": identity_confidence,
        "knownContact": known_contact,
        "contactName": contact_name,
        "senderNumber": normalized_event.get("sender_number"),
        "recipientNumber": normalized_event.get("recipient_number"),
        "lineDisplay": line_display or normalized_event.get("line_display"),
        "eventType": normalized_event.get("event_type") or "sms",
        "evidence": sorted(set(evidence)),
        "recency": recency,
        "contextDraftAllowed": context_draft_allowed,
        "genericDraftAllowed": generic_draft_allowed,
        "draftMode": "context_aware" if context_draft_allowed else (
            "deterministic_fallback" if generic_draft_allowed else "none"
        ),
    }


def _extract_call_history_row(call):
    from_number = _pick_nested(
        call,
        [
            ("external_number",),
            ("from_number",),
            ("contact", "phone"),
            ("contact", "phone_number"),
            ("contact", "number"),
        ],
    )
    to_number = _pick_nested(
        call,
        [
            ("entry_point_target", "phone"),
            ("target", "phone"),
            ("proxy_target", "phone"),
            ("internal_number",),
            ("to_number",),
        ],
    )
    line_name = _pick_nested(
        call,
        [
            ("entry_point_target", "name"),
            ("target", "name"),
            ("proxy_target", "name"),
        ],
    )
    started_ms = _parse_timestamp_ms(
        _pick_nested(
            call,
            [
                ("date_started",),
                ("started_at",),
                ("start_time",),
                ("date_created",),
            ],
        )
    )
    direction = str(_pick_nested(call, [("direction",)]) or "").lower()
    state = str(_pick_nested(call, [("state",)]) or "").lower()
    duration_raw = _pick_nested(call, [("duration",), ("total_duration",)])
    try:
        duration = int(float(str(duration_raw)))
    except (TypeError, ValueError):
        duration = None
    return {
        "from_number": from_number,
        "to_number": to_number,
        "line_name": line_name,
        "started_ms": started_ms,
        "direction": direction,
        "state": state,
        "duration": duration,
    }


def _fetch_recent_calls_around(event_ts_ms, window_ms=30 * 60 * 1000, limit=25):
    if not DIALPAD_API_KEY or event_ts_ms is None:
        return []

    params = {
        "started_after": str(max(0, event_ts_ms - window_ms)),
        "started_before": str(event_ts_ms + window_ms),
        "limit": str(limit),
    }
    url = f"{CALLS_ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {DIALPAD_API_KEY}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"⚠️  Missed-call history lookup failed: {e}")
        return []

    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return [item for item in payload["items"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def resolve_missed_call_context(data, history_fetcher=None):
    """
    Resolve missed-call caller/line data with deterministic fallbacks.
    Resolution path priority: payload_direct -> payload_inferred -> history_backfill -> unresolved
    """
    payload_direct_from = extract_number(data, "from_number", "caller_number", "from")
    payload_direct_to = extract_number(data, "to_number", "called_number", "to")

    payload_inferred_from = _pick_nested(
        data,
        [
            ("from", "number"),
            ("call", "from_number"),
            ("call", "from", "number"),
            ("event", "from_number"),
            ("event", "call", "from_number"),
            ("customer", "phone"),
            ("contact", "phone"),
        ],
    )
    payload_inferred_to = _pick_nested(
        data,
        [
            ("to", "number"),
            ("call", "to_number"),
            ("call", "to", "number"),
            ("event", "to_number"),
            ("event", "call", "to_number"),
            ("entry_point_target", "phone"),
            ("target", "phone"),
            ("proxy_target", "phone"),
            ("line", "phone"),
        ],
    )
    payload_inferred_line_name = _pick_nested(
        data,
        [
            ("line", "name"),
            ("call", "line", "name"),
            ("entry_point_target", "name"),
            ("target", "name"),
            ("proxy_target", "name"),
        ],
    )

    from_number = payload_direct_from or payload_inferred_from
    to_number = payload_direct_to or payload_inferred_to
    caller_path = "payload_direct" if payload_direct_from else ("payload_inferred" if payload_inferred_from else "unresolved")
    line_path = "payload_direct" if payload_direct_to else ("payload_inferred" if (payload_inferred_to or payload_inferred_line_name) else "unresolved")

    event_ts_ms = _parse_timestamp_ms(
        _pick_nested(
            data,
            [
                ("date_started",),
                ("date_start",),
                ("start_time",),
                ("timestamp",),
                ("event_timestamp",),
                ("event", "timestamp"),
                ("call", "date_started"),
            ],
        )
    )

    if caller_path == "unresolved" or line_path == "unresolved":
        fetcher = history_fetcher or _fetch_recent_calls_around
        history_rows = fetcher(event_ts_ms) if event_ts_ms is not None else []
        best = None
        best_key = None
        caller_norm = normalize_phone_number(from_number)
        line_norm = normalize_phone_number(to_number)
        for call in history_rows:
            row = _extract_call_history_row(call)
            score = 0
            is_inbound = row["direction"] == "inbound"
            is_missed_like = row["state"] in MISSED_CALL_STATES or row["duration"] == 0
            if is_inbound:
                score += 1
            if is_missed_like:
                score += 1
            row_from_norm = normalize_phone_number(row["from_number"])
            row_to_norm = normalize_phone_number(row["to_number"])
            caller_match = bool(caller_norm and row_from_norm and caller_norm == row_from_norm)
            line_match = bool(line_norm and row_to_norm and line_norm == row_to_norm)
            if caller_match:
                score += 3
            if line_match:
                score += 3
            row["_missed_inbound"] = bool(is_inbound and is_missed_like)
            row["_has_match_evidence"] = bool(caller_match or line_match)
            time_delta = abs((row["started_ms"] or event_ts_ms) - event_ts_ms) if event_ts_ms is not None else 0
            ranking = (1 if row["_missed_inbound"] else 0, 1 if row["_has_match_evidence"] else 0, score, -time_delta)
            if best is None or ranking > best_key:
                best = row
                best_key = ranking

        if best and best.get("_missed_inbound") and best.get("_has_match_evidence"):
            if caller_path == "unresolved" and best.get("from_number"):
                from_number = best["from_number"]
                caller_path = "history_backfill"
            if line_path == "unresolved" and (best.get("to_number") or best.get("line_name")):
                if best.get("to_number"):
                    to_number = best["to_number"]
                if not payload_inferred_line_name and best.get("line_name"):
                    payload_inferred_line_name = best["line_name"]
                line_path = "history_backfill"

    line_display = get_line_name(to_number)
    if not line_display and payload_inferred_line_name:
        line_display = payload_inferred_line_name
        if line_path == "unresolved":
            line_path = "payload_inferred"
    if not line_display:
        inferred_legacy_line = infer_line_display_from_payload(data)
        if inferred_legacy_line:
            line_display = inferred_legacy_line
            if line_path == "unresolved":
                line_path = "payload_inferred"

    return {
        "from_number": from_number or "Unknown",
        "to_number": to_number,
        "line_display": line_display,
        "event_ts_ms": event_ts_ms,
        "caller_resolution_path": caller_path,
        "line_resolution_path": line_path,
    }


def normalize_sms_payload(data, contact_info=None):
    """Normalize Dialpad webhook payload to a consistent SMS object for hooks."""
    sender_number = data.get("from_number")
    recipient_number = _first_value(data.get("to_number"))
    text = data.get("text", data.get("text_content", "")) or ""
    direction = data.get("direction", "unknown")
    timestamp = (
        data.get("timestamp")
        or data.get("event_timestamp")
        or data.get("created_date")
        or data.get("date_created")
    )
    conversation_id = data.get("conversation_id")
    message_id = data.get("message_id") or data.get("id")

    contact_name = contact_info or (data.get("contact", {}) or {}).get("name")
    sender = contact_name or sender_number or "Unknown"

    return {
        "event_type": "sms",
        "sender": sender,
        "sender_number": sender_number,
        "recipient_number": recipient_number,
        "text": text,
        "timestamp": timestamp,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "direction": direction,
    }


def normalize_call_hook_payload(data, resolved_context, contact_info=None):
    """Normalize missed-call context to a consistent hook event object."""
    sender_number = resolved_context.get("from_number")
    recipient_number = resolved_context.get("to_number")
    timestamp = resolved_context.get("event_ts_ms") or (
        data.get("date_started")
        or data.get("date_start")
        or data.get("start_time")
        or data.get("timestamp")
    )
    call_id = data.get("call_id") or data.get("id")
    line_display = resolved_context.get("line_display") or get_line_name(recipient_number)
    sender = contact_info or sender_number or "Unknown"

    return {
        "event_type": "missed_call",
        "sender": sender,
        "sender_number": sender_number,
        "recipient_number": recipient_number,
        "timestamp": timestamp,
        "call_id": call_id,
        "line_display": line_display,
        "direction": str(data.get("call_direction", data.get("direction", "unknown"))).lower(),
    }


def build_first_contact_context(normalized_event, sender_enrichment=None, line_display=None):
    """Build a compact first-contact hint for downstream operator assist."""
    event_type = normalized_event.get("event_type") or "sms"
    if event_type not in {"sms", "missed_call", "voicemail"}:
        return None

    sender_enrichment = sender_enrichment or {}
    contact_name = sender_enrichment.get("contact_name")
    contact_name_text = str(contact_name or "").strip()
    lookup_status = str(sender_enrichment.get("status") or "not_applicable")
    payload_contact_only = bool(sender_enrichment.get("payload_contact_used")) and lookup_status != "resolved"
    known_contact = (
        bool(contact_name_text)
        and contact_name_text.lower() != "unknown"
        and lookup_status == "resolved"
        and not payload_contact_only
    )
    first_contact_candidate = not known_contact
    if sender_enrichment.get("degraded") or lookup_status in {"disabled", "not_applicable"}:
        identity_state = "degraded"
    elif payload_contact_only:
        identity_state = "payload_contact"
    elif known_contact:
        identity_state = "resolved"
    else:
        identity_state = lookup_status

    return {
        "knownContact": known_contact,
        "needsIdentityLookup": first_contact_candidate,
        "needsBusinessContext": first_contact_candidate,
        "needsDraftReply": first_contact_candidate,
        "needsDialpadContactSync": first_contact_candidate,
        "keepBrief": not first_contact_candidate,
        "identityState": identity_state,
        "contactName": contact_name,
        "senderNumber": normalized_event.get("sender_number"),
        "recipientNumber": normalized_event.get("recipient_number"),
        "lineDisplay": line_display or normalized_event.get("line_display"),
        "eventType": event_type,
        "lookup": {
            "status": lookup_status,
            "degraded": bool(sender_enrichment.get("degraded")),
            "degradedReason": sender_enrichment.get("degraded_reason"),
        },
    }


def build_proactive_reply_message(normalized_event, sender_enrichment=None):
    """Build the sales-line auto-reply message for first-contact inbound events."""
    sender_enrichment = sender_enrichment or {}
    inbound_context = normalized_event.get("inbound_context") or {}
    contact_name = None
    if inbound_context.get("identityConfidence") != "low":
        contact_name = sender_enrichment.get("first_name") or sender_enrichment.get("contact_name")
    if contact_name:
        greeting_name = str(contact_name).strip().split()[0]
    else:
        greeting_name = "there"

    event_type = normalized_event.get("event_type") or "sms"
    if inbound_context.get("contextDraftAllowed"):
        if event_type == "missed_call":
            body = "sorry we missed your call. I saw your recent ShapeScale conversation and can help from here. What would you like to cover?"
        elif event_type == "voicemail":
            body = "thanks for the voicemail. I saw your recent ShapeScale conversation and will follow up shortly."
        else:
            body = "thanks for reaching out. I saw your recent ShapeScale conversation and will follow up shortly."
    elif event_type == "missed_call":
        body = "you've reached ShapeScale for Business Sales. Sorry we missed your call. How can we help?"
    elif event_type == "voicemail":
        body = "thanks for the voicemail. We received it and will be in touch shortly."
    else:
        body = "thanks for reaching ShapeScale for Business Sales. We got your message and will be in touch shortly."

    return f"Hi {greeting_name}, {body}"


def should_send_proactive_reply(normalized_event, sender_enrichment=None, line_display=None):
    """Return True when the sales-line auto-reply should be sent."""
    if not DIALPAD_AUTO_REPLY_ENABLED:
        return False

    sender_number = normalized_event.get("sender_number")
    recipient_number = normalized_event.get("recipient_number")
    if not sender_number or not recipient_number:
        return False

    if normalize_phone_number(recipient_number) != DIALPAD_AUTO_REPLY_SALES_LINE:
        return False

    first_contact = normalized_event.get("first_contact") or build_first_contact_context(
        normalized_event,
        sender_enrichment=sender_enrichment,
        line_display=line_display,
    )
    if not first_contact:
        return False

    lookup = first_contact.get("lookup") or {}
    if lookup.get("degraded"):
        return False

    inbound_context = normalized_event.get("inbound_context")
    if inbound_context is None:
        inbound_context = build_inbound_context(
            normalized_event,
            sender_enrichment=sender_enrichment,
            line_display=line_display,
        )
    if inbound_context.get("contextDraftAllowed"):
        return True

    return _generic_fallback_draft_allowed(normalized_event, first_contact)


def summarize_message_status(result):
    """Normalize Dialpad SMS send status for auto-reply logging."""
    if not isinstance(result, dict):
        return "unknown", None

    raw_status = result.get("message_status")
    if raw_status is None:
        raw_status = result.get("status")
    if raw_status is None:
        return "unknown", None

    raw_text = str(raw_status).strip()
    if not raw_text:
        return "unknown", None

    normalized = "accepted/queued" if raw_text.lower() == "pending" else raw_text
    return normalized, raw_text


def send_proactive_reply(normalized_event, sender_enrichment=None, line_display=None):
    """Deprecated direct-send path retained as a safe no-op."""
    if not should_send_proactive_reply(
        normalized_event,
        sender_enrichment=sender_enrichment,
        line_display=line_display,
    ):
        return False, "not_eligible", None
    message = build_proactive_reply_message(normalized_event, sender_enrichment=sender_enrichment)
    return False, "approval_required", message


def build_approval_review_suffix(draft_id, draft_message, reply_policy=None):
    """Build Telegram review text for an approval draft without implying a send."""
    if not draft_id or not draft_message:
        return ""

    reply_policy = reply_policy or {}
    risk_state = reply_policy.get("state")
    buttons_enabled = build_sms_approval_reply_markup(draft_id, reply_policy) is not None
    lines = [
        "",
        "",
        "📝 *SMS approval draft \\(not sent\\)*",
        f"*Draft ID:* `{escape_telegram_markdown(draft_id)}`",
        f"*Exact text:*\n{escape_telegram_markdown(draft_message)}",
    ]
    if buttons_enabled:
        lines.extend(["", "Use the Telegram buttons below to approve or reject this exact draft."])
    else:
        lines.extend(
            [
                "",
                f"Approve from an operator shell: `bin/approve_sms_draft.py {escape_telegram_markdown(draft_id)} --actor-id <human-id> --approval-token \"$DIALPAD_SMS_APPROVAL_TOKEN\" --json`",
            ]
        )
    if risk_state == "risky":
        reason = reply_policy.get("risk_reason") or "risk policy matched"
        lines.extend(
            [
                "",
                f"⚠️ *Risk:* {escape_telegram_markdown(reason)}",
            ]
        )
        if buttons_enabled:
            lines.append("Approval is two-step: first acknowledge risk, then confirm send.")
        else:
            lines.append(
                f"Second confirmation required: `bin/approve_sms_draft.py {escape_telegram_markdown(draft_id)} --action confirm-risk --actor-id <human-id> --approval-token \"$DIALPAD_SMS_APPROVAL_TOKEN\" --json`"
            )
    return "\n".join(lines)


def build_inbound_context_brief(inbound_context, auto_reply_status=None, auto_reply_draft_created=False):
    """Build a compact Telegram context/provenance block."""
    if not inbound_context:
        return ""

    contact_name = inbound_context.get("contactName")
    identity = contact_name or "Unknown / first contact"
    confidence = inbound_context.get("identityConfidence") or "unknown"
    evidence = inbound_context.get("evidence") or []
    recency = inbound_context.get("recency") or {}
    recency_state = recency.get("state") or "unknown"
    age_days = recency.get("ageDays")
    if age_days is not None:
        recency_text = f"{recency_state} ({age_days} days old)"
    else:
        recency_text = recency_state

    draft_allowed = inbound_context.get("contextDraftAllowed")
    generic_draft_allowed = inbound_context.get("genericDraftAllowed")
    draft_mode = inbound_context.get("draftMode") or "none"
    if auto_reply_draft_created:
        mode_text = "context-aware" if draft_mode == "context_aware" else "generic fallback"
        draft_text = f"approval draft created ({mode_text})"
    elif auto_reply_status and auto_reply_status not in {"draft_created", "approval_required"}:
        draft_text = f"no approval draft ({auto_reply_status})"
    elif draft_allowed:
        draft_text = "context-aware approval draft eligible"
    elif generic_draft_allowed:
        draft_text = "generic approval draft eligible"
    else:
        draft_text = "no approval draft"

    evidence_text = ", ".join(str(item).replace("_", " ") for item in evidence) or "none"
    lines = [
        "",
        "",
        "🔎 *Inbound context*",
        f"*Identity:* {escape_telegram_markdown(identity)} \\({escape_telegram_markdown(confidence)}\\)",
        f"*Evidence:* {escape_telegram_markdown(evidence_text)}",
        f"*Recency:* {escape_telegram_markdown(recency_text)}",
        f"*Draft basis:* {escape_telegram_markdown(draft_text)}",
    ]
    return "\n".join(lines)


def build_human_only_blocked_suffix(reply_policy=None):
    """Build Telegram text when policy blocks SMS automation outright."""
    reply_policy = reply_policy or {}
    if reply_policy.get("state") != "blocked_opt_out":
        return ""

    reason = reply_policy.get("risk_reason") or "automation is blocked for this thread"
    lines = [
        "",
        "",
        f"🛑 *{escape_telegram_markdown('Automation blocked / human-only')}*",
        escape_telegram_markdown("No SMS approval draft was created."),
        f"*Reason:* {escape_telegram_markdown(reason)}",
    ]
    return "\n".join(lines)


def invalidate_pending_sms_drafts(thread_key=None, customer_number=None, reason="new_inbound"):
    """Stale pending approval drafts when newer inbound context makes them unsafe."""
    if sms_approval is None or (not thread_key and not customer_number):
        return False

    try:
        conn = sms_approval.init_db()
        try:
            if thread_key:
                sms_approval.invalidate_pending(conn, thread_key=thread_key, reason=reason)
            if customer_number:
                sms_approval.invalidate_pending(conn, customer_number=customer_number, reason=reason)
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 - webhook must degrade safely.
        print(f"⚠️  Failed to invalidate pending SMS approvals ({type(exc).__name__})")
        return False


def mark_opt_out_fail_closed(customer_number, *, reason="customer_opt_out", source=None):
    """Persist opt-out, falling back to an emergency block before returning success."""
    if sms_approval is None or not customer_number:
        return False

    try:
        conn = sms_approval.init_db()
        try:
            sms_approval.mark_opt_out(
                conn,
                customer_number=customer_number,
                reason=reason,
                source=source,
            )
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 - explicit opt-outs must fail closed.
        print(f"⚠️  Failed to persist opt-out ({type(exc).__name__})")

    invalidate_pending_sms_drafts(customer_number=customer_number, reason=reason)
    try:
        sms_approval.record_emergency_opt_out(
            customer_number=customer_number,
            reason=reason,
            source=source,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - webhook must degrade safely.
        print(f"⚠️  Failed to record emergency opt-out ({type(exc).__name__})")
        return False


def create_proactive_reply_draft(normalized_event, sender_enrichment=None, line_display=None):
    """Create an approval-gated proactive reply draft instead of sending SMS."""
    sender_number = normalized_event.get("recipient_number")
    recipient_number = normalized_event.get("sender_number")
    thread_key = build_hook_session_key(normalized_event)
    reply_policy = classify_sms_reply_policy(normalized_event.get("text") or "")
    if reply_policy["state"] == "blocked_opt_out":
        opt_out_blocked = mark_opt_out_fail_closed(
            recipient_number,
            reason="customer_opt_out",
            source=normalized_event.get("event_type"),
        )
        if not opt_out_blocked:
            return False, "opt_out_persistence_failed", None, None, {
                **reply_policy,
                "risk_reason": "explicit opt-out could not be made durable",
            }
        return False, "blocked_opt_out", None, None, reply_policy

    if not should_send_proactive_reply(
        normalized_event,
        sender_enrichment=sender_enrichment,
        line_display=line_display,
    ):
        invalidate_pending_sms_drafts(
            thread_key=thread_key,
            customer_number=recipient_number,
            reason="new_inbound_not_eligible",
        )
        return False, "not_eligible", None, None, None

    message = build_proactive_reply_message(normalized_event, sender_enrichment=sender_enrichment)
    if sms_approval is None:
        return False, "approval_unavailable", message, None, reply_policy
    if not sender_number or not recipient_number:
        return False, "missing_sender_or_recipient", message, None, reply_policy

    risk_state = (
        sms_approval.RISK_RISKY
        if reply_policy["state"] == "risky"
        else sms_approval.RISK_NORMAL
    )
    context_fingerprint = sms_approval.build_context_fingerprint(
        {
            "thread_key": thread_key,
            "sender": sender_number,
            "recipient": recipient_number,
            "message_id": normalized_event.get("message_id") or normalized_event.get("call_id"),
            "line_display": line_display or normalized_event.get("line_display"),
            "first_contact": normalized_event.get("first_contact"),
            "inbound_context": normalized_event.get("inbound_context"),
        }
    )
    try:
        conn = sms_approval.init_db()
        try:
            if sms_approval.is_opted_out(conn, recipient_number):
                return False, "blocked_opt_out", message, None, {
                    "state": "blocked_opt_out",
                    "reason_code": "filtered_opt_out",
                    "risk_reason": "customer previously opted out",
                }
            draft = sms_approval.create_replacement_draft(
                conn,
                invalidate_thread_key=thread_key,
                invalidate_customer_number=recipient_number,
                thread_key=thread_key,
                customer_number=recipient_number,
                sender_number=sender_number,
                draft_text=message,
                source_inbound_id=normalized_event.get("message_id") or normalized_event.get("call_id"),
                risk_state=risk_state,
                risk_reason=reply_policy.get("risk_reason"),
                context_fingerprint=context_fingerprint,
                metadata={
                    "event_type": normalized_event.get("event_type"),
                    "line_display": line_display or normalized_event.get("line_display"),
                    "draft_mode": (normalized_event.get("inbound_context") or {}).get("draftMode"),
                },
            )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - webhook should not fail because approval storage is down.
        print(f"⚠️  Approval draft persistence failed ({type(exc).__name__})")
        return False, "approval_persistence_failed", message, None, reply_policy

    return True, "draft_created", message, draft.get("draft_id"), reply_policy


def build_hook_session_key(normalized_event):
    """Build stable OpenClaw hook session key with fallbacks."""
    event_type = normalized_event.get("event_type") or "sms"
    if event_type == "missed_call":
        call_id = normalized_event.get("call_id")
        if call_id:
            return f"hook:dialpad:call:{call_id}"

        sender_number = normalize_phone_number(normalized_event.get("sender_number"))
        timestamp = normalized_event.get("timestamp")
        if sender_number and timestamp is not None:
            return f"hook:dialpad:call:{sender_number}:{timestamp}"
        if timestamp is not None:
            return f"hook:dialpad:call:{timestamp}"
        if sender_number:
            return f"hook:dialpad:call:{sender_number}"
        return "hook:dialpad:call:unknown"

    sender_number = normalize_phone_number(normalized_event.get("sender_number"))
    recipient_number = normalize_phone_number(normalized_event.get("recipient_number"))
    candidate = normalized_event.get("conversation_id")
    if not candidate and sender_number and recipient_number:
        candidate = f"{sender_number}:{recipient_number}"
    if not candidate:
        candidate = normalized_event.get("message_id") or sender_number or "unknown"
    return f"hook:dialpad:sms:{candidate}"


def format_hook_message(normalized_event, line_display=None):
    """Build hook message text for a normalized OpenClaw hook event."""
    sender = normalized_event.get("sender") or "Unknown"
    sender_number = normalized_event.get("sender_number") or "Unknown"
    recipient_number = normalized_event.get("recipient_number")
    timestamp = normalized_event.get("timestamp")
    event_type = normalized_event.get("event_type") or "sms"
    resolved_line = line_display or normalized_event.get("line_display")

    if event_type == "missed_call":
        lines = ["📞 Dialpad Missed Call", f"From: {sender} ({sender_number})"]
        if resolved_line:
            lines.append(f"Line: {resolved_line}")
        elif recipient_number:
            lines.append(f"Line: {recipient_number}")
        if timestamp is not None:
            lines.append(f"Time: {timestamp}")
        call_id = normalized_event.get("call_id")
        if call_id:
            lines.append(f"Call ID: {call_id}")
        return "\n".join(lines)

    body = normalized_event.get("text", "")
    lines = ["📩 Dialpad SMS", f"From: {sender} ({sender_number})"]
    if resolved_line:
        lines.append(f"To: {resolved_line}")
    elif recipient_number:
        lines.append(f"To: {recipient_number}")
    if timestamp is not None:
        lines.append(f"Time: {timestamp}")
    lines.append("")
    lines.append(f"Message: {body}")
    return "\n".join(lines)


def get_openclaw_hooks_url():
    """Build complete OpenClaw hooks URL from gateway + path env vars."""
    return f"{OPENCLAW_GATEWAY_URL.rstrip('/')}/{OPENCLAW_HOOKS_PATH.lstrip('/')}"


def build_openclaw_hook_payload(normalized_event, line_display=None):
    """Build /hooks/agent payload for a normalized hook event."""
    event_type = normalized_event.get("event_type") or "sms"
    hook_name = OPENCLAW_HOOKS_NAME
    if event_type == "missed_call":
        hook_name = OPENCLAW_HOOKS_CALL_NAME

    payload = {
        "message": format_hook_message(normalized_event, line_display=line_display),
        "name": hook_name,
        "sessionKey": build_hook_session_key(normalized_event),
        "deliver": True,
    }

    sender_number_normalized = normalize_phone_number(normalized_event.get("sender_number"))
    target_to = OPENCLAW_HOOKS_TO
    if (
        DIALPAD_PRIORITY_ROUTE_TO
        and sender_number_normalized
        and sender_number_normalized in PRIORITY_ROUTE_PHONES
    ):
        target_to = DIALPAD_PRIORITY_ROUTE_TO

    if OPENCLAW_HOOKS_CHANNEL:
        payload["channel"] = OPENCLAW_HOOKS_CHANNEL
    if target_to:
        payload["to"] = target_to
    if OPENCLAW_HOOKS_AGENT_ID:
        payload["agentId"] = OPENCLAW_HOOKS_AGENT_ID

    first_contact = normalized_event.get("first_contact")
    if first_contact is None and normalized_event.get("sender_enrichment"):
        first_contact = build_first_contact_context(
            normalized_event,
            sender_enrichment=normalized_event.get("sender_enrichment"),
            line_display=line_display,
        )
    if first_contact is not None:
        payload["firstContact"] = first_contact

    inbound_context = normalized_event.get("inbound_context")
    if inbound_context is not None:
        payload["inboundContext"] = inbound_context

    auto_reply = normalized_event.get("auto_reply")
    if auto_reply is not None:
        payload["autoReply"] = auto_reply

    return payload


def send_to_openclaw_hooks(normalized_event, line_display=None):
    """
    Forward a normalized event payload to OpenClaw hooks.
    Returns (success: bool, status: str).
    """
    event_type = normalized_event.get("event_type") or "sms"
    hooks_enabled = OPENCLAW_HOOKS_SMS_ENABLED
    event_label = "SMS"
    if event_type == "missed_call":
        hooks_enabled = OPENCLAW_HOOKS_CALL_ENABLED
        event_label = "missed call"

    if not hooks_enabled:
        print(f"⚠️  OpenClaw {event_label} hook forwarding disabled by config")
        return False, "disabled_by_config"

    if not OPENCLAW_HOOKS_TOKEN:
        print(f"⚠️  OPENCLAW_HOOKS_TOKEN is not configured ({event_label} hooks forwarding disabled)")
        return False, "token_missing"

    payload = build_openclaw_hook_payload(normalized_event, line_display=line_display)

    url = get_openclaw_hooks_url()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENCLAW_HOOKS_TOKEN}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            status_code = getattr(response, "status", 200)
            if 200 <= status_code < 300:
                return True, f"http_{status_code}"
            return False, f"http_{status_code}"
    except Exception as e:
        print(f"❌ Error forwarding {event_label} to OpenClaw hooks: {e}")
        return False, "request_failed"


def send_sms_to_openclaw_hooks(normalized_sms, line_display=None):
    """
    Forward normalized SMS payload to OpenClaw hooks.
    Returns (success: bool, status: str).
    """
    return send_to_openclaw_hooks(normalized_sms, line_display=line_display)


class DialpadWebhookHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Dialpad webhooks"""

    def do_GET(self):
        """Handle GET requests (health check only)"""
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        """Handle POST requests"""
        log_line(f"➡️  HTTP POST {self.path} from={self.client_address[0]}")

        # /store endpoint - called by OpenClaw plugin to store messages
        if self.path == "/store":
            self.handle_store()
            return

        # /webhook/dialpad - main webhook endpoint
        if self.path == "/webhook/dialpad":
            self.handle_webhook()
            return

        # /webhook/telegram - Telegram inline approval callbacks
        if self.path == "/webhook/telegram":
            self.handle_telegram_webhook()
            return

        # /webhook/dialpad-call - missed call notifications
        if self.path == "/webhook/dialpad-call":
            self.handle_call_webhook()
            return

        # /webhook/dialpad-voicemail - voicemail notifications
        if self.path == "/webhook/dialpad-voicemail":
            self.handle_voicemail_webhook()
            return

        self.send_error(404, "Not Found")

    def send_json_response(self, status_code, payload):
        """Send a JSON response."""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def read_json_body(self, endpoint_label):
        """Read a bounded JSON request body."""
        max_body_size = 1024 * 1024
        try:
            requested_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            requested_length = 0
        content_length = min(requested_length, max_body_size)
        raw_body = self.rfile.read(content_length)
        try:
            return raw_body, json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except json.JSONDecodeError as exc:
            print(f"❌ Invalid JSON payload on {endpoint_label}: {exc}")
            return raw_body, None

    def handle_telegram_webhook(self):
        """Handle Telegram inline button callback updates."""
        _raw_body, data = self.read_json_body("/webhook/telegram")
        if data is None:
            self.send_error(400, "Invalid JSON")
            return

        if not TELEGRAM_WEBHOOK_SECRET:
            log_line("❌ Telegram webhook secret is not configured")
            self.send_json_response(503, {"status": "misconfigured", "reason": "secret_missing"})
            return

        provided_secret = _get_header(self.headers, "X-Telegram-Bot-Api-Secret-Token")
        if not provided_secret or not hmac.compare_digest(
            str(provided_secret),
            str(TELEGRAM_WEBHOOK_SECRET),
        ):
            log_line("❌ Unauthorized Telegram webhook request")
            self.send_error(401, "Unauthorized")
            return

        callback_query = data.get("callback_query")
        if not isinstance(callback_query, dict):
            self.send_json_response(200, {"status": "ignored", "reason": "not_callback_query"})
            return

        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if str(chat_id) != str(TELEGRAM_CHAT_ID):
            answer_telegram_callback(
                callback_query.get("id"),
                "This approval button is not valid in this chat.",
                show_alert=True,
            )
            self.send_error(403, "Forbidden")
            return

        parsed = parse_telegram_callback_data(callback_query.get("data"))
        if not parsed:
            answer_telegram_callback(
                callback_query.get("id"),
                "Invalid approval action.",
                show_alert=True,
            )
            self.send_json_response(200, {"status": "invalid_callback"})
            return

        result = dispatch_telegram_approval_callback(callback_query, parsed)
        answer_telegram_callback(
            callback_query.get("id"),
            callback_answer_text(result),
            show_alert=not bool(result.get("ok")),
        )
        update_telegram_review_after_callback(callback_query, result, parsed["draft_id"])
        self.send_json_response(
            200,
            {
                "status": "ok",
                "approval_status": result.get("status"),
                "sent": bool(result.get("sent")),
                "draft_id": parsed["draft_id"],
            },
        )

    def handle_store(self):
        """Handle /store endpoint - stores message in SQLite, called by OpenClaw plugin"""
        # Limit request body size to prevent memory exhaustion (1MB max)
        MAX_BODY_SIZE = 1024 * 1024  # 1MB
        content_length = min(int(self.headers.get("Content-Length", 0)), MAX_BODY_SIZE)
        body = self.rfile.read(content_length).decode("utf-8")

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            print(f"❌ Invalid JSON on /store: {e}")
            self.send_error(400, "Invalid JSON")
            return

        try:
            result = handle_sms_webhook(data)
            stored = result.get("stored", False)

            self.send_response(200 if stored else 500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        except Exception as e:
            print(f"❌ Storage error on /store: {e}")
            self.send_error(500, f"Storage error: {e}")

    def handle_webhook(self):
        """Handle /webhook/dialpad endpoint - main Dialpad webhook"""
        # Limit request body size to prevent memory exhaustion (1MB max)
        MAX_BODY_SIZE = 1024 * 1024  # 1MB
        content_length = min(int(self.headers.get("Content-Length", 0)), MAX_BODY_SIZE)
        raw_body = self.rfile.read(content_length)

        auth_ok, auth_source = verify_webhook_auth(self.headers, raw_body, WEBHOOK_SECRET)
        if not auth_ok:
            log_line("❌ Unauthorized webhook request on /webhook/dialpad")
            self.send_error(401, "Unauthorized")
            return

        body = raw_body.decode("utf-8")
        log_line(
            f"📥 /webhook/dialpad hit bytes={len(raw_body)} auth={auth_source} "
            f"ua={_get_header(self.headers, 'User-Agent') or 'unknown'}"
        )

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            print(f"❌ Invalid JSON payload: {e}")
            self.send_error(400, "Invalid JSON")
            return

        timestamp = datetime.now().isoformat()
        direction = str(data.get("direction", "unknown")).lower()
        from_num = first_value(data.get("from_number")) or "N/A"
        to_num = data.get("to_number")
        text = extract_message_text(data)
        notification_type = classify_inbound_notification(data) if direction == "inbound" else "not_inbound"

        # Store message in SQLite
        try:
            result = handle_sms_webhook(data)
            stored = result.get("stored", False)

            if not stored:
                print(f"⚠️  Failed to store message: {result.get('error', 'Unknown error')}")
                self.send_error(500, "Storage failed")
                return

        except Exception as e:
            print(f"❌ Storage error: {e}")
            self.send_error(500, f"Storage error: {e}")
            return

        # Forward inbound SMS to OpenClaw hooks (non-sensitive only)
        hook_sent = False
        hook_status = None
        auto_reply_sent = False
        auto_reply_status = None
        auto_reply_draft_id = None
        sensitive_filtered = False
        inbound_alert_decision = {
            "eligible": False,
            "reason_code": "not_inbound",
            "sensitive_filtered": False,
            "notification_type": notification_type,
        }
        sender_enrichment = {
            "contact_name": None,
            "status": "not_applicable",
            "degraded": False,
            "degraded_reason": None,
        }
        if direction == "inbound":
            # Resolve contact name before filtering so sender check isn't "Unknown"
            sender_enrichment = lookup_contact_enrichment(from_num)
            sender_enrichment = apply_payload_contact_fallback(sender_enrichment, data)
            contact_info = sender_enrichment.get("contact_name")
            if not contact_info and result.get("message"):
                cached = result["message"].get("contact_name", "")
                if cached and cached != "Unknown":
                    contact_info = cached
                    sender_enrichment.setdefault("payload_contact_name", cached)
                    sender_enrichment["payload_contact_used"] = True
                    if sender_enrichment.get("status") in {None, "not_found", "not_applicable", "disabled"}:
                        sender_enrichment["status"] = "payload_contact"
            sender_enrichment["contact_name"] = contact_info

            inbound_alert_decision = assess_inbound_sms_alert_eligibility(
                data,
                from_number=from_num,
                text=text,
                sender=contact_info or "",
                notification_type=notification_type,
            )
            hook_status = inbound_alert_decision["reason_code"]
            sensitive_filtered = inbound_alert_decision["sensitive_filtered"]

            if not inbound_alert_decision["eligible"] and hook_status != "filtered_opt_out":
                invalidate_pending_sms_drafts(
                    customer_number=from_num,
                    reason=f"new_inbound_{hook_status}",
                )

            if inbound_alert_decision["eligible"]:
                line_display = get_line_name(to_num)
                normalized_sms = normalize_sms_payload(data, contact_info=contact_info)
                normalized_sms["first_contact"] = build_first_contact_context(
                    normalized_sms,
                    sender_enrichment=sender_enrichment,
                    line_display=line_display,
                )
                recent_context = lookup_recent_sms_context(
                    from_num,
                    current_dialpad_id=normalized_sms.get("message_id"),
                    current_timestamp_ms=_parse_timestamp_ms(normalized_sms.get("timestamp")),
                )
                normalized_sms["inbound_context"] = build_inbound_context(
                    normalized_sms,
                    sender_enrichment=sender_enrichment,
                    line_display=line_display,
                    recent_context=recent_context,
                )
                auto_reply_eligible = should_send_proactive_reply(
                    normalized_sms,
                    sender_enrichment=sender_enrichment,
                    line_display=line_display,
                )
                auto_reply_draft_created, auto_reply_status, auto_reply_message, auto_reply_draft_id, reply_policy = create_proactive_reply_draft(
                    normalized_sms,
                    sender_enrichment=sender_enrichment,
                    line_display=line_display,
                )
                normalized_sms["auto_reply"] = {
                    "eligible": auto_reply_eligible,
                    "sent": False,
                    "draftCreated": auto_reply_draft_created,
                    "draftId": auto_reply_draft_id,
                    "status": auto_reply_status,
                    "message": auto_reply_message,
                    "replyPolicy": reply_policy,
                }
                hook_sent, hook_status = send_sms_to_openclaw_hooks(
                    normalized_sms, line_display=line_display
                )
                if auto_reply_status:
                    print(f"   🤖 Auto Reply Draft: {'✓' if auto_reply_draft_created else '✗'} ({auto_reply_status})")
            elif hook_status == "filtered_shortcode":
                print("   🔒 Short-code message filtered (not forwarding to OpenClaw hooks)")
            elif hook_status == "filtered_sensitive":
                print("   🔒 Sensitive message filtered (not forwarding to OpenClaw hooks)")
            elif hook_status == "filtered_opt_out":
                print("   🛑 Opt-out message filtered (automation send path blocked)")
                opt_out_blocked = mark_opt_out_fail_closed(
                    from_num,
                    reason="customer_opt_out",
                    source="sms",
                )
                if not opt_out_blocked:
                    hook_status = "opt_out_persistence_failed"
        elif direction == "outbound" and sms_approval is not None:
            outbound_customers = to_num if isinstance(to_num, list) else [to_num]
            outbound_customers = [customer for customer in outbound_customers if customer]
            if outbound_customers:
                try:
                    conn = sms_approval.init_db()
                    try:
                        for outbound_customer in outbound_customers:
                            sms_approval.invalidate_pending(
                                conn,
                                customer_number=outbound_customer,
                                reason="manual_outbound",
                            )
                    finally:
                        conn.close()
                except Exception as exc:  # noqa: BLE001 - webhook must degrade safely.
                    print(f"⚠️  Failed to invalidate approvals after outbound SMS ({type(exc).__name__})")
        # Optional immediate Telegram notification for inbound SMS
        telegram_sms_sent = None
        telegram_status = TELEGRAM_STATUS_NOT_APPLICABLE
        if direction == "inbound":
            if (
                inbound_alert_decision["eligible"]
                and DIALPAD_SMS_TELEGRAM_NOTIFY
            ):
                line_display = get_line_name(to_num)
                to_display = line_display or str(first_value(to_num) or "Unknown")
                contact_info = sender_enrichment.get("contact_name")
                from_display = f"{contact_info} ({from_num})" if contact_info else str(from_num)
                time_display = datetime.now().strftime("%I:%M %p").lstrip("0")
                tg_text = (
                    "📩 Dialpad SMS\n"
                    f"From: {escape_telegram_markdown(from_display)}\n"
                    f"To: {escape_telegram_markdown(to_display)}\n"
                    f"Time: {escape_telegram_markdown(time_display)}\n\n"
                    f"Message: {escape_telegram_markdown(text)}"
                )
                tg_text += build_inbound_context_brief(
                    normalized_sms.get("inbound_context"),
                    auto_reply_status=auto_reply_status,
                    auto_reply_draft_created=auto_reply_draft_created,
                )
                tg_text += build_approval_review_suffix(
                    auto_reply_draft_id,
                    auto_reply_message,
                    reply_policy,
                )
                tg_text += build_human_only_blocked_suffix(reply_policy)
                reply_markup = build_sms_approval_reply_markup(auto_reply_draft_id, reply_policy)
                telegram_sms_sent = (
                    send_to_telegram(tg_text, reply_markup=reply_markup)
                    if reply_markup
                    else send_to_telegram(tg_text)
                )
                telegram_status = TELEGRAM_STATUS_SENT if telegram_sms_sent else TELEGRAM_STATUS_FAILED
            elif hook_status == "filtered_opt_out":
                tg_text = (
                    "🛑 Dialpad SMS opt-out / human-only\n"
                    f"From: {escape_telegram_markdown(str(from_num))}\n"
                    "Automation is not allowed to send on this thread."
                )
                telegram_sms_sent = send_to_telegram(tg_text)
                telegram_status = "human_only_notified" if telegram_sms_sent else TELEGRAM_STATUS_FAILED
            elif hook_status == "opt_out_persistence_failed":
                tg_text = (
                    "🛑 Dialpad SMS opt-out persistence failed / human-only\n"
                    f"From: {escape_telegram_markdown(str(from_num))}\n"
                    "Automation did not create a draft, but the opt-out could not be confirmed durable."
                )
                telegram_sms_sent = send_to_telegram(tg_text)
                telegram_status = "opt_out_persistence_failed" if telegram_sms_sent else TELEGRAM_STATUS_FAILED
            elif not DIALPAD_SMS_TELEGRAM_NOTIFY:
                telegram_status = "disabled"
            else:
                telegram_status = inbound_alert_decision["reason_code"]

        # Console logging
        print(f"[{timestamp}]")
        print(f"   📱 {direction.upper()}: {from_num}")
        if text:
            text_preview = text[:60] + "..." if len(text) > 60 else text
            print(f"   📄 \"{text_preview}\"")
        print(f"   💾 Stored: ✓")
        if WEBHOOK_SECRET:
            print(f"   🔐 Auth: ✓ ({auth_source})")
        if direction == "inbound":
            print(
                "   🧭 Inbound Alert Eligibility: "
                f"{'allow' if inbound_alert_decision['eligible'] else 'block'} "
                f"({inbound_alert_decision['reason_code']})"
            )
            if sensitive_filtered:
                print(f"   🪝 OpenClaw Hook: ✗ ({hook_status} — filtered)")
            else:
                print(f"   🪝 OpenClaw Hook: {'✓' if hook_sent else '✗'} ({hook_status})")
            if telegram_sms_sent is not None:
                print(f"   📨 Telegram SMS Alert: {'✓' if telegram_sms_sent else '✗'} ({telegram_status})")
            else:
                print(f"   📨 Telegram SMS Alert: ✗ ({telegram_status})")
            if auto_reply_status is not None and not auto_reply_sent:
                print(f"   🤖 Auto Reply: ✗ ({auto_reply_status})")
            if sender_enrichment.get("degraded"):
                print(
                    "   ⚠️  Sender enrichment degraded "
                    f"({sender_enrichment.get('degraded_reason')})"
                )
        print()

        # Always return 200 OK (graceful degradation)
        # Webhook succeeded even if hook forwarding failed
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {
            "status": "ok",
            "stored": True,
            "hook_forwarded": hook_sent if direction == "inbound" else None,
            "hook_status": hook_status if direction == "inbound" else None,
            "inbound_alert_eligible": (
                inbound_alert_decision.get("eligible") if direction == "inbound" else None
            ),
            "inbound_alert_reason": (
                inbound_alert_decision.get("reason_code") if direction == "inbound" else None
            ),
            "telegram_status": telegram_status if direction == "inbound" else None,
            "auto_reply_sent": auto_reply_sent if direction == "inbound" else None,
            "auto_reply_status": auto_reply_status if direction == "inbound" else None,
            "auto_reply_draft_id": auto_reply_draft_id if direction == "inbound" else None,
            "sender_enrichment_degraded": (
                sender_enrichment.get("degraded") if direction == "inbound" else None
            ),
            "sender_enrichment_degraded_reason": (
                sender_enrichment.get("degraded_reason") if direction == "inbound" else None
            ),
            "sender_enrichment_status": (
                sender_enrichment.get("status") if direction == "inbound" else None
            ),
        }
        self.wfile.write(json.dumps(response).encode())

    def handle_call_webhook(self):
        """Handle /webhook/dialpad-call endpoint - missed call notifications"""
        # Limit request body size to prevent memory exhaustion (1MB max)
        MAX_BODY_SIZE = 1024 * 1024  # 1MB
        content_length = min(int(self.headers.get("Content-Length", 0)), MAX_BODY_SIZE)
        raw_body = self.rfile.read(content_length)

        auth_ok, auth_source = verify_webhook_auth(self.headers, raw_body, WEBHOOK_SECRET)
        if not auth_ok:
            log_line("❌ Unauthorized webhook request on /webhook/dialpad-call")
            self.send_error(401, "Unauthorized")
            return

        body = raw_body.decode("utf-8")

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            print(f"❌ Invalid JSON payload on /webhook/dialpad-call: {e}")
            self.send_error(400, "Invalid JSON")
            return

        direction = data.get("call_direction", data.get("direction", "unknown"))
        call_missed = data.get("call_missed", False)
        raw_duration = data.get("duration", data.get("call_duration", 0))
        try:
            duration = int(float(raw_duration))
        except (TypeError, ValueError):
            duration = 0
        call_state = str(data.get("call_state", "")).lower()

        should_notify = (
            direction == "inbound" and (
                call_missed is True or
                duration == 0 or
                call_state == "missed"
            )
        )

        hook_sent = False
        hook_status = None
        telegram_sent = False
        auto_reply_sent = False
        auto_reply_status = None
        auto_reply_draft_id = None
        duplicate = False
        missed_call_dedupe_key = None
        missed_call_dedupe_status = None
        if should_notify:
            resolved = resolve_missed_call_context(data)
            from_num = resolved["from_number"]
            to_num = resolved["to_number"]
            call_ts = resolved["event_ts_ms"] or (
                data.get("date_started") or
                data.get("date_start") or
                data.get("start_time") or
                data.get("timestamp")
            )
            missed_call_dedupe_key = build_missed_call_dedupe_key(data, resolved)
            dedupe_claim = claim_missed_call_notification(missed_call_dedupe_key)
            duplicate = bool(dedupe_claim.get("duplicate"))
            missed_call_dedupe_status = dedupe_claim.get("status")
            if duplicate:
                print(f"[{datetime.now().isoformat()}]")
                print(f"   📞 MISSED CALL duplicate suppressed: {from_num} -> {resolved['line_display'] or get_line_name(to_num) or 'Unknown'}")
                print(f"   🧷 Dedupe: {missed_call_dedupe_key} ({missed_call_dedupe_status})")
                call_id = data.get("call_id") or data.get("id")
                entry_point_call_id = data.get("entry_point_call_id")
                if call_id:
                    print(f"   📞 Call ID: {call_id}")
                if entry_point_call_id:
                    print(f"   📞 Entry point call ID: {entry_point_call_id}")
                print()

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                response = {
                    "status": "ok",
                    "missed_call": True,
                    "duplicate": True,
                    "dedupe_key": missed_call_dedupe_key,
                    "dedupe_status": missed_call_dedupe_status,
                    "hook_forwarded": False,
                    "hook_status": "duplicate_suppressed",
                    "auto_reply_sent": False,
                    "auto_reply_status": "duplicate_suppressed",
                    "auto_reply_draft_id": None,
                    "telegram_sent": False,
                }
                self.wfile.write(json.dumps(response).encode())
                return

            sender_enrichment = (
                lookup_contact_enrichment(from_num) if from_num != "Unknown" else {
                    "contact_name": None,
                    "status": "not_applicable",
                    "degraded": False,
                    "degraded_reason": None,
                }
            )
            sender_enrichment = apply_payload_contact_fallback(sender_enrichment, data)
            contact_info = sender_enrichment.get("contact_name")
            line_display = resolved["line_display"] or get_line_name(to_num)
            to_display = line_display if line_display else "Unknown"
            if contact_info:
                from_display = f"*{contact_info}* (`{from_num}`)"
            elif from_num == "Unknown":
                from_display = "Unknown"
            else:
                from_display = f"`{from_num}`"
            time_display = datetime.now().strftime("%I:%M %p").lstrip("0")
            if call_ts is not None:
                try:
                    time_display = datetime.fromtimestamp(
                        int(call_ts) / 1000
                    ).astimezone().strftime("%I:%M %p").lstrip("0")
                except (TypeError, ValueError, OSError, OverflowError):
                    pass

            tg_text = (
                f"📞 *Missed Call*\n"
                f"*Line:* {escape_telegram_markdown(to_display)}\n"
                f"*From:* {escape_telegram_markdown(from_display)}\n"
                f"*Time:* {escape_telegram_markdown(time_display)}"
            )

            normalized_event = normalize_call_hook_payload(
                data,
                resolved,
                contact_info=contact_info,
            )
            normalized_event["first_contact"] = build_first_contact_context(
                normalized_event,
                sender_enrichment=sender_enrichment,
                line_display=line_display,
            )
            recent_context = lookup_recent_call_context(
                from_num,
                line_number=to_num,
                event_ts_ms=_parse_timestamp_ms(normalized_event.get("timestamp")),
            )
            normalized_event["inbound_context"] = build_inbound_context(
                normalized_event,
                sender_enrichment=sender_enrichment,
                line_display=line_display,
                recent_context=recent_context,
            )
            auto_reply_eligible = should_send_proactive_reply(
                normalized_event,
                sender_enrichment=sender_enrichment,
                line_display=line_display,
            )
            auto_reply_draft_created, auto_reply_status, auto_reply_message, auto_reply_draft_id, reply_policy = create_proactive_reply_draft(
                normalized_event,
                sender_enrichment=sender_enrichment,
                line_display=line_display,
            )
            normalized_event["auto_reply"] = {
                "eligible": auto_reply_eligible,
                "sent": False,
                "draftCreated": auto_reply_draft_created,
                "draftId": auto_reply_draft_id,
                "status": auto_reply_status,
                "message": auto_reply_message,
                "replyPolicy": reply_policy,
            }
            hook_sent, hook_status = send_to_openclaw_hooks(
                normalized_event,
                line_display=line_display,
            )
            tg_text += build_inbound_context_brief(
                normalized_event.get("inbound_context"),
                auto_reply_status=auto_reply_status,
                auto_reply_draft_created=auto_reply_draft_created,
            )
            tg_text += build_approval_review_suffix(
                auto_reply_draft_id,
                auto_reply_message,
                reply_policy,
            )
            tg_text += build_human_only_blocked_suffix(reply_policy)
            reply_markup = build_sms_approval_reply_markup(auto_reply_draft_id, reply_policy)
            telegram_sent = (
                send_to_telegram(tg_text, reply_markup=reply_markup)
                if reply_markup
                else send_to_telegram(tg_text)
            )

            print(f"[{datetime.now().isoformat()}]")
            print(f"   📞 MISSED CALL: {from_num} -> {to_display}")
            if WEBHOOK_SECRET:
                print(f"   🔐 Auth: ✓ ({auth_source})")
            if call_ts:
                print(f"   🕒 Event time: {call_ts}")
            print(f"   🧷 Dedupe: {missed_call_dedupe_key} ({missed_call_dedupe_status})")
            call_id = data.get("call_id") or data.get("id")
            entry_point_call_id = data.get("entry_point_call_id")
            if call_id:
                print(f"   📞 Call ID: {call_id}")
            if entry_point_call_id:
                print(f"   📞 Entry point call ID: {entry_point_call_id}")
            print(
                "   🔎 Resolution: "
                f"caller={resolved['caller_resolution_path']}, "
                f"line={resolved['line_resolution_path']}"
            )
            if auto_reply_status is not None:
                print(f"   🤖 Auto Reply: {'✓' if auto_reply_sent else '✗'} ({auto_reply_status})")
            print(f"   🪝 OpenClaw Hook: {'✓' if hook_sent else '✗'} ({hook_status})")
            print(f"   📨 Telegram: {'✓' if telegram_sent else '✗'}")
            print()
        else:
            print(f"[{datetime.now().isoformat()}]")
            print(f"   📞 CALL EVENT ignored (not inbound missed call)")
            if WEBHOOK_SECRET:
                print(f"   🔐 Auth: ✓ ({auth_source})")
            print()

        # Always return 200 OK (graceful degradation)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {
            "status": "ok",
            "missed_call": should_notify,
            "duplicate": duplicate if should_notify else False,
            "dedupe_key": missed_call_dedupe_key if should_notify else None,
            "dedupe_status": missed_call_dedupe_status if should_notify else None,
            "hook_forwarded": hook_sent if should_notify else None,
            "hook_status": hook_status if should_notify else None,
            "auto_reply_sent": auto_reply_sent if should_notify else None,
            "auto_reply_status": auto_reply_status if should_notify else None,
            "auto_reply_draft_id": auto_reply_draft_id if should_notify else None,
            "telegram_sent": telegram_sent if should_notify else None
        }
        self.wfile.write(json.dumps(response).encode())

    def handle_voicemail_webhook(self):
        """Handle /webhook/dialpad-voicemail endpoint - voicemail notifications"""
        # Limit request body size to prevent memory exhaustion (1MB max)
        MAX_BODY_SIZE = 1024 * 1024  # 1MB
        content_length = min(int(self.headers.get("Content-Length", 0)), MAX_BODY_SIZE)
        raw_body = self.rfile.read(content_length)

        auth_ok, auth_source = verify_webhook_auth(self.headers, raw_body, WEBHOOK_SECRET)
        if not auth_ok:
            log_line("❌ Unauthorized webhook request on /webhook/dialpad-voicemail")
            self.send_error(401, "Unauthorized")
            return

        body = raw_body.decode("utf-8")

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            print(f"❌ Invalid JSON payload on /webhook/dialpad-voicemail: {e}")
            self.send_error(400, "Invalid JSON")
            return

        from_num = extract_number(data, "from_number", "caller_number", "from") or "Unknown"
        to_num = extract_number(data, "to_number", "called_number", "to")
        duration = data.get("duration", data.get("voicemail_duration", 0))
        transcription = data.get("voicemail_transcription") or data.get("transcription")

        sender_enrichment = (
            lookup_contact_enrichment(from_num) if from_num != "Unknown" else {
                "contact_name": None,
                "first_name": None,
                "last_name": None,
                "company": None,
                "job_title": None,
                "status": "not_applicable",
                "degraded": False,
                "degraded_reason": None,
            }
        )
        contact_info = sender_enrichment.get("contact_name")
        line_display = get_line_name(to_num)
        to_display = line_display if line_display else "Unknown"
        if contact_info:
            from_display = f"*{contact_info}* (`{from_num}`)"
        elif from_num == "Unknown":
            from_display = "Unknown"
        else:
            from_display = f"`{from_num}`"
        try:
            duration_seconds = int(float(duration))
        except (TypeError, ValueError):
            duration_seconds = 0
        duration_display = f"{duration_seconds}s"

        tg_text = (
            f"📬 *New Voicemail*\n"
            f"*To:* {to_display}\n"
            f"*From:* {from_display}\n"
            f"*Duration:* {duration_display}"
        )

        if transcription:
            tg_text += (
                f"\n\n"
                f"*Transcription:*\n"
                f"_\"{transcription}\"_"
            )

        auto_reply_sent = False
        auto_reply_status = None
        auto_reply_draft_id = None
        normalized_event = {
            "event_type": "voicemail",
            "sender": contact_info or from_num or "Unknown",
            "sender_number": from_num,
            "recipient_number": to_num,
            "text": transcription or "",
            "timestamp": data.get("timestamp") or data.get("created_date"),
            "line_display": line_display,
            "direction": "inbound",
        }
        normalized_event["first_contact"] = build_first_contact_context(
            normalized_event,
            sender_enrichment=sender_enrichment,
            line_display=line_display,
        )
        auto_reply_eligible = should_send_proactive_reply(
            normalized_event,
            sender_enrichment=sender_enrichment,
            line_display=line_display,
        )
        auto_reply_draft_created, auto_reply_status, auto_reply_message, auto_reply_draft_id, reply_policy = create_proactive_reply_draft(
            normalized_event,
            sender_enrichment=sender_enrichment,
            line_display=line_display,
        )
        normalized_event["auto_reply"] = {
            "eligible": auto_reply_eligible,
            "sent": False,
            "draftCreated": auto_reply_draft_created,
            "draftId": auto_reply_draft_id,
            "status": auto_reply_status,
            "message": auto_reply_message,
            "replyPolicy": reply_policy,
        }
        tg_text += build_approval_review_suffix(
            auto_reply_draft_id,
            auto_reply_message,
            reply_policy,
        )
        tg_text += build_human_only_blocked_suffix(reply_policy)
        reply_markup = build_sms_approval_reply_markup(auto_reply_draft_id, reply_policy)
        telegram_sent = (
            send_to_telegram(tg_text, reply_markup=reply_markup)
            if reply_markup
            else send_to_telegram(tg_text)
        )

        print(f"[{datetime.now().isoformat()}]")
        print(f"   📬 VOICEMAIL: {from_num} -> {to_display}")
        if WEBHOOK_SECRET:
            print(f"   🔐 Auth: ✓ ({auth_source})")
        print(f"   ⏱️  Duration: {duration_display}")
        if transcription:
            trans_preview = transcription[:80] + "..." if len(transcription) > 80 else transcription
            print(f"   📝 Transcription: \"{trans_preview}\"")
        if auto_reply_status is not None:
            print(f"   🤖 Auto Reply: {'✓' if auto_reply_sent else '✗'} ({auto_reply_status})")
        print(f"   📨 Telegram: {'✓' if telegram_sent else '✗'}")
        print()

        # Always return 200 OK (graceful degradation)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {
            "status": "ok",
            "voicemail": True,
            "telegram_sent": telegram_sent,
            "auto_reply_sent": auto_reply_sent,
            "auto_reply_status": auto_reply_status,
            "auto_reply_draft_id": auto_reply_draft_id,
        }
        self.wfile.write(json.dumps(response).encode())

    def log_message(self, format, *args):
        """Suppress default HTTP logging (we do our own)"""
        pass


def main():
    """Start the webhook server"""
    server = HTTPServer(("0.0.0.0", PORT), DialpadWebhookHandler)

    log_line("=" * 60)
    print("🚀 Dialpad SMS Webhook Server (OpenClaw Hooks)")
    print("=" * 60)
    print(f"Port: {PORT}")
    print(f"Endpoints:")
    print(f"  - POST /webhook/dialpad (main webhook)")
    print(f"  - POST /webhook/telegram (Telegram approval callbacks)")
    print(f"  - POST /webhook/dialpad-call (missed call webhook)")
    print(f"  - POST /webhook/dialpad-voicemail (voicemail webhook)")
    print(f"  - GET  /health (health check)")
    print(f"")
    print(f"Configuration:")
    print(f"  - Dialpad API: {'✓' if DIALPAD_API_KEY else '✗ (contact lookup disabled)'}")
    print(f"  - OpenClaw Gateway URL: {OPENCLAW_GATEWAY_URL}")
    print(f"  - OpenClaw Hooks Path: {OPENCLAW_HOOKS_PATH}")
    print(f"  - OpenClaw Hooks Token: {'✓' if OPENCLAW_HOOKS_TOKEN else '✗ (hook forwarding disabled)'}")
    print(f"  - Telegram Approval Buttons: {'✓' if telegram_buttons_available() else '✗ (disabled or incomplete config)'}")
    print(f"  - OpenClaw Hooks Name: {OPENCLAW_HOOKS_NAME}")
    print(f"  - OpenClaw Call Hooks Name: {OPENCLAW_HOOKS_CALL_NAME}")
    print(f"  - OpenClaw Hooks Channel: {OPENCLAW_HOOKS_CHANNEL or '(unset)'}")
    print(f"  - OpenClaw Hooks To: {OPENCLAW_HOOKS_TO or '(unset)'}")
    print(f"  - OpenClaw SMS Hooks Enabled: {'✓' if OPENCLAW_HOOKS_SMS_ENABLED else '✗'}")
    print(f"  - OpenClaw Call Hooks Enabled: {'✓' if OPENCLAW_HOOKS_CALL_ENABLED else '✗'}")
    print(f"  - Priority Route To: {DIALPAD_PRIORITY_ROUTE_TO or '(unset)'}")
    if PRIORITY_ROUTE_PHONES:
        print(f"  - Priority Route Phones: {', '.join(sorted(PRIORITY_ROUTE_PHONES))}")
    else:
        print(f"  - Priority Route Phones: (unset)")
    print(f"  - OpenClaw Hooks Agent ID: {OPENCLAW_HOOKS_AGENT_ID or '(default)'}")
    print(f"  - Telegram: {'✓' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else '✗ (call/voicemail notifications disabled)'}")
    print(f"  - SMS Telegram Alerts: {'✓' if DIALPAD_SMS_TELEGRAM_NOTIFY else '✗ (disabled)'}")
    tg_ready = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    print(f"  - Call Notifications: {'✓' if tg_ready else '✗ (Telegram not fully configured)'}")
    print(f"  - Voicemail Notifications: {'✓' if tg_ready else '✗ (Telegram not fully configured)'}")
    print(f"  - Webhook Verification: {'✓' if WEBHOOK_SECRET else '✗ (disabled)'}")
    print(f"  - Line Names:")
    for number in sorted(LINE_NAMES.keys()):
        formatted = format_phone_number(number) or number
        print(f"    - {LINE_NAMES[number]}: {formatted}")
    print("=" * 60)
    print("Press Ctrl+C to stop")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
