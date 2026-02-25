#!/usr/bin/env python3
"""
Dialpad SMS Webhook Server with SQLite Storage and OpenClaw Hooks SMS Ingress

Receives Dialpad SMS events, stores in SQLite with FTS5 search, and sends
OpenClaw hook messages for inbound SMS.

Features:
- SQLite storage with FTS5 full-text search (via webhook_sqlite.py)
- OpenClaw hooks forwarding for inbound SMS with contact name resolution
- Telegram notifications for missed calls and voicemails
- Health check endpoint
- Graceful error handling (hook/Telegram failures don't break webhooks)
- Zero external dependencies (stdlib only)
- All secrets from environment variables

Environment Variables:
- PORT (default: 8081) - HTTP server port
- DIALPAD_TELEGRAM_BOT_TOKEN - Telegram bot token (required for call/voicemail notifications)
- DIALPAD_TELEGRAM_CHAT_ID - Telegram chat ID (required for call/voicemail notifications)
- DIALPAD_API_KEY - Dialpad API key (required for contact lookup)
- DIALPAD_WEBHOOK_SECRET - webhook auth secret (optional, enables signature/JWT verification)
- OPENCLAW_GATEWAY_URL (default: http://127.0.0.1:8080)
- OPENCLAW_HOOKS_TOKEN (required for SMS hook forwarding)
- OPENCLAW_HOOKS_PATH (default: /hooks/agent)
- OPENCLAW_HOOKS_NAME (default: Dialpad SMS)
- OPENCLAW_HOOKS_CHANNEL (optional)
- OPENCLAW_HOOKS_TO (optional)
- OPENCLAW_HOOKS_AGENT_ID (optional)
"""

import json
import os
import sys
import hmac
import hashlib
import base64
import binascii
import re
import urllib.request
import urllib.parse
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Add skill directory to path for local imports
skill_dir = Path(__file__).parent
sys.path.insert(0, str(skill_dir))

# Import existing SQLite storage handler
from webhook_sqlite import handle_sms_webhook

# Environment configuration (NO HARDCODED SECRETS)
PORT = int(os.environ.get("PORT", "8081"))
WEBHOOK_SECRET = os.environ.get("DIALPAD_WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("DIALPAD_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("DIALPAD_TELEGRAM_CHAT_ID", "")
DIALPAD_API_KEY = os.environ.get("DIALPAD_API_KEY", "")
DIALPAD_LINE_NAMES = os.environ.get("DIALPAD_LINE_NAMES", "")
OPENCLAW_GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:8080")
OPENCLAW_HOOKS_TOKEN = os.environ.get("OPENCLAW_HOOKS_TOKEN", "")
OPENCLAW_HOOKS_PATH = os.environ.get("OPENCLAW_HOOKS_PATH", "/hooks/agent")
OPENCLAW_HOOKS_NAME = os.environ.get("OPENCLAW_HOOKS_NAME", "Dialpad SMS")
OPENCLAW_HOOKS_CHANNEL = os.environ.get("OPENCLAW_HOOKS_CHANNEL", "")
OPENCLAW_HOOKS_TO = os.environ.get("OPENCLAW_HOOKS_TO", "")
OPENCLAW_HOOKS_AGENT_ID = os.environ.get("OPENCLAW_HOOKS_AGENT_ID", "")

DEFAULT_LINE_NAMES = {
    "+14155201316": "Sales",
    "+14153602954": "Work",
    "+14159917155": "Support",
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

TELEGRAM_STATUS_SENT = "sent"
TELEGRAM_STATUS_FILTERED = "filtered"
TELEGRAM_STATUS_NOT_APPLICABLE = "not_applicable"
TELEGRAM_STATUS_FAILED = "failed"

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
    except Exception as e:
        print(f"⚠️  Invalid DIALPAD_LINE_NAMES, using defaults: {e}")

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


def get_contact_name(phone_number):
    """
    Try to resolve a phone number to a contact name via Dialpad API.
    Returns None if lookup fails or API key is missing.
    """
    if not DIALPAD_API_KEY:
        return None

    search_url = f"https://dialpad.com/api/v2/contacts?query={urllib.parse.quote(phone_number)}"
    headers = {
        "Authorization": f"Bearer {DIALPAD_API_KEY}",
        "Accept": "application/json"
    }

    try:
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            items = data.get("items", [])
            if items:
                c = items[0]
                name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
                company = c.get('company', '')
                title = c.get('job_title', '')
                info = name or "Known Contact"
                if company:
                    info += f" ({company})"
                if title:
                    info = f"{title} | {info}"
                return info
    except Exception as e:
        print(f"⚠️  Dialpad contact lookup failed: {e}")
    return None


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


def first_value(value):
    """Return first item for list-like values, otherwise passthrough."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


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

    from_num = first_value(data.get("from_number"))
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


def send_to_telegram(text):
    """
    Send a message to the configured Telegram channel.
    Returns True on success, False on failure (non-blocking).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram not configured (missing BOT_TOKEN or CHAT_ID)")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return True
    except Exception as e:
        print(f"❌ Error sending to Telegram: {e}")
        return False


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
        "sender": sender,
        "sender_number": sender_number,
        "recipient_number": recipient_number,
        "text": text,
        "timestamp": timestamp,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "direction": direction,
    }


def build_hook_session_key(normalized_sms):
    """Build stable OpenClaw hook session key with fallbacks."""
    candidate = (
        normalized_sms.get("conversation_id")
        or normalized_sms.get("message_id")
        or normalize_phone_number(normalized_sms.get("sender_number"))
        or "unknown"
    )
    return f"hook:dialpad:sms:{candidate}"


def format_hook_message(normalized_sms, line_display=None):
    """Build hook message text with short metadata and body."""
    sender = normalized_sms.get("sender") or "Unknown"
    sender_number = normalized_sms.get("sender_number") or "Unknown"
    recipient_number = normalized_sms.get("recipient_number")
    timestamp = normalized_sms.get("timestamp")
    body = normalized_sms.get("text", "")
    message_id = normalized_sms.get("message_id")

    lines = ["Dialpad inbound SMS"]
    if line_display:
        lines.append(f"To Line: {line_display}")
    elif recipient_number:
        lines.append(f"To: {recipient_number}")
    lines.append(f"From: {sender} ({sender_number})")
    if timestamp is not None:
        lines.append(f"Timestamp: {timestamp}")
    if message_id is not None:
        lines.append(f"Message ID: {message_id}")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def get_openclaw_hooks_url():
    """Build complete OpenClaw hooks URL from gateway + path env vars."""
    return f"{OPENCLAW_GATEWAY_URL.rstrip('/')}/{OPENCLAW_HOOKS_PATH.lstrip('/')}"


def build_openclaw_hook_payload(normalized_sms, line_display=None):
    """Build /hooks/agent payload for a normalized inbound SMS."""
    payload = {
        "message": format_hook_message(normalized_sms, line_display=line_display),
        "name": OPENCLAW_HOOKS_NAME,
        "sessionKey": build_hook_session_key(normalized_sms),
        "deliver": True,
    }
    if OPENCLAW_HOOKS_CHANNEL:
        payload["channel"] = OPENCLAW_HOOKS_CHANNEL
    if OPENCLAW_HOOKS_TO:
        payload["to"] = OPENCLAW_HOOKS_TO
    if OPENCLAW_HOOKS_AGENT_ID:
        payload["agentId"] = OPENCLAW_HOOKS_AGENT_ID
    return payload


def send_sms_to_openclaw_hooks(normalized_sms, line_display=None):
    """
    Forward normalized SMS payload to OpenClaw hooks.
    Returns (success: bool, status: str).
    """
    if not OPENCLAW_HOOKS_TOKEN:
        print("⚠️  OPENCLAW_HOOKS_TOKEN is not configured (SMS hooks forwarding disabled)")
        return False, "token_missing"

    payload = build_openclaw_hook_payload(normalized_sms, line_display=line_display)

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
        print(f"❌ Error forwarding inbound SMS to OpenClaw hooks: {e}")
        return False, "request_failed"


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
        # /store endpoint - called by OpenClaw plugin to store messages
        if self.path == "/store":
            self.handle_store()
            return

        # /webhook/dialpad - main webhook endpoint
        if self.path == "/webhook/dialpad":
            self.handle_webhook()
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
            print("❌ Unauthorized webhook request on /webhook/dialpad")
            self.send_error(401, "Unauthorized")
            return

        body = raw_body.decode("utf-8")

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
        sensitive_filtered = False
        if direction == "inbound":
            # Resolve contact name before filtering so sender check isn't "Unknown"
            contact_info = get_contact_name(from_num)
            if not contact_info and result.get("message"):
                cached = result["message"].get("contact_name", "")
                if cached and cached != "Unknown":
                    contact_info = cached

            notification_type = classify_inbound_notification(data)
            if notification_type == "missed_call":
                hook_status = "filtered_missed_call"
            elif notification_type == "blank_sms":
                hook_status = "filtered_blank_sms"
            elif is_sensitive_message(text=text, sender=contact_info or "", contact_number=from_num):
                sensitive_filtered = True
                hook_status = "filtered_sensitive"
                print("   🔒 Sensitive message filtered (not forwarding to OpenClaw hooks)")
            else:
                line_display = get_line_name(to_num)
                normalized_sms = normalize_sms_payload(data, contact_info=contact_info)
                hook_sent, hook_status = send_sms_to_openclaw_hooks(
                    normalized_sms, line_display=line_display
                )
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
            if sensitive_filtered:
                print("   🪝 OpenClaw Hook: ✗ (sensitive — filtered)")
            else:
                print(f"   🪝 OpenClaw Hook: {'✓' if hook_sent else '✗'} ({hook_status})")
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
        }
        self.wfile.write(json.dumps(response).encode())

    def handle_call_webhook(self):
        """Handle /webhook/dialpad-call endpoint - missed call notifications"""
        # Limit request body size to prevent memory exhaustion (1MB max)
        MAX_BODY_SIZE = 1024 * 1024  # 1MB
        content_length = min(int(self.headers.get("Content-Length", 0)), MAX_BODY_SIZE)
        body = self.rfile.read(content_length).decode("utf-8")

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

        telegram_sent = False
        if should_notify:
            from_num = data.get("from_number") or "Unknown"
            to_num = data.get("to_number")
            call_ts = (
                data.get("date_started") or
                data.get("date_start") or
                data.get("start_time") or
                data.get("timestamp")
            )
            contact_info = get_contact_name(from_num) if from_num != "Unknown" else None
            line_display = get_line_name(to_num)
            to_display = line_display if line_display else "Unknown"
            if contact_info:
                from_display = f"*{contact_info}* (`{from_num}`)"
            elif from_num == "Unknown":
                from_display = "Unknown"
            else:
                from_display = f"`{from_num}`"
            time_display = datetime.now().strftime("%I:%M %p").lstrip("0")

            tg_text = (
                f"📞 *Missed Call*\n"
                f"*To:* {to_display}\n"
                f"*From:* {from_display}\n"
                f"*Time:* {time_display}"
            )
            telegram_sent = send_to_telegram(tg_text)

            print(f"[{datetime.now().isoformat()}]")
            print(f"   📞 MISSED CALL: {from_num} -> {to_display}")
            if call_ts:
                print(f"   🕒 Event time: {call_ts}")
            print(f"   📨 Telegram: {'✓' if telegram_sent else '✗'}")
            print()
        else:
            print(f"[{datetime.now().isoformat()}]")
            print(f"   📞 CALL EVENT ignored (not inbound missed call)")
            print()

        # Always return 200 OK (graceful degradation)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {
            "status": "ok",
            "missed_call": should_notify,
            "telegram_sent": telegram_sent if should_notify else None
        }
        self.wfile.write(json.dumps(response).encode())

    def handle_voicemail_webhook(self):
        """Handle /webhook/dialpad-voicemail endpoint - voicemail notifications"""
        # Limit request body size to prevent memory exhaustion (1MB max)
        MAX_BODY_SIZE = 1024 * 1024  # 1MB
        content_length = min(int(self.headers.get("Content-Length", 0)), MAX_BODY_SIZE)
        body = self.rfile.read(content_length).decode("utf-8")

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            print(f"❌ Invalid JSON payload on /webhook/dialpad-voicemail: {e}")
            self.send_error(400, "Invalid JSON")
            return

        from_num = data.get("from_number") or "Unknown"
        to_num = data.get("to_number")
        duration = data.get("duration", data.get("voicemail_duration", 0))
        transcription = data.get("voicemail_transcription") or data.get("transcription")

        contact_info = get_contact_name(from_num) if from_num != "Unknown" else None
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

        telegram_sent = send_to_telegram(tg_text)

        print(f"[{datetime.now().isoformat()}]")
        print(f"   📬 VOICEMAIL: {from_num} -> {to_display}")
        print(f"   ⏱️  Duration: {duration_display}")
        if transcription:
            trans_preview = transcription[:80] + "..." if len(transcription) > 80 else transcription
            print(f"   📝 Transcription: \"{trans_preview}\"")
        print(f"   📨 Telegram: {'✓' if telegram_sent else '✗'}")
        print()

        # Always return 200 OK (graceful degradation)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {
            "status": "ok",
            "voicemail": True,
            "telegram_sent": telegram_sent
        }
        self.wfile.write(json.dumps(response).encode())

    def log_message(self, format, *args):
        """Suppress default HTTP logging (we do our own)"""
        pass


def main():
    """Start the webhook server"""
    server = HTTPServer(("0.0.0.0", PORT), DialpadWebhookHandler)

    print("=" * 60)
    print("🚀 Dialpad SMS Webhook Server (OpenClaw Hooks)")
    print("=" * 60)
    print(f"Port: {PORT}")
    print(f"Endpoints:")
    print(f"  - POST /webhook/dialpad (main webhook)")
    print(f"  - POST /webhook/dialpad-call (missed call webhook)")
    print(f"  - POST /webhook/dialpad-voicemail (voicemail webhook)")
    print(f"  - GET  /health (health check)")
    print(f"")
    print(f"Configuration:")
    print(f"  - Dialpad API: {'✓' if DIALPAD_API_KEY else '✗ (contact lookup disabled)'}")
    print(f"  - OpenClaw Gateway URL: {OPENCLAW_GATEWAY_URL}")
    print(f"  - OpenClaw Hooks Path: {OPENCLAW_HOOKS_PATH}")
    print(f"  - OpenClaw Hooks Token: {'✓' if OPENCLAW_HOOKS_TOKEN else '✗ (SMS hook forwarding disabled)'}")
    print(f"  - OpenClaw Hooks Name: {OPENCLAW_HOOKS_NAME}")
    print(f"  - OpenClaw Hooks Channel: {OPENCLAW_HOOKS_CHANNEL or '(unset)'}")
    print(f"  - OpenClaw Hooks To: {OPENCLAW_HOOKS_TO or '(unset)'}")
    print(f"  - OpenClaw Hooks Agent ID: {OPENCLAW_HOOKS_AGENT_ID or '(default)'}")
    print(f"  - Telegram: {'✓' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else '✗ (call/voicemail notifications disabled)'}")
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
