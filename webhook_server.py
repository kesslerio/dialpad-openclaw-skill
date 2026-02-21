#!/usr/bin/env python3
"""
Dialpad SMS Webhook Server with SQLite Storage and Telegram Integration

Receives Dialpad SMS events, stores in SQLite with FTS5 search, and sends
Telegram notifications for inbound messages.

Features:
- SQLite storage with FTS5 full-text search (via webhook_sqlite.py)
- Telegram notifications with contact name resolution
- Health check endpoint
- Graceful error handling (Telegram failures don't break webhooks)
- Zero external dependencies (stdlib only)
- All secrets from environment variables

Environment Variables:
- PORT (default: 8081) - HTTP server port
- DIALPAD_TELEGRAM_BOT_TOKEN - Telegram bot token (required for notifications)
- DIALPAD_TELEGRAM_CHAT_ID - Telegram chat ID (required for notifications)
- DIALPAD_API_KEY - Dialpad API key (required for contact lookup)
- DIALPAD_WEBHOOK_SECRET - JWT signature secret (optional)
"""

import json
import os
import sys
import hmac
import hashlib
import urllib.request
import urllib.parse
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Add skill directory to path for local imports
skill_dir = Path(__file__).parent
sys.path.insert(0, str(skill_dir))

# Import existing SQLite storage handler
from webhook_sqlite import handle_sms_webhook, format_notification

from sms_filter_compat import is_sensitive_message

# Environment configuration (NO HARDCODED SECRETS)
PORT = int(os.environ.get("PORT", "8081"))
WEBHOOK_SECRET = os.environ.get("DIALPAD_WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("DIALPAD_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("DIALPAD_TELEGRAM_CHAT_ID", "")
DIALPAD_API_KEY = os.environ.get("DIALPAD_API_KEY", "")
DIALPAD_LINE_NAMES = os.environ.get("DIALPAD_LINE_NAMES", "")

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


def verify_jwt(payload_b64, signature, secret):
    """
    Verify JWT signature if secret is configured.
    Returns True if valid or secret not configured (permissive).
    """
    if not secret:
        return True

    expected = hmac.new(
        secret.encode(),
        payload_b64.encode(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


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
        body = self.rfile.read(content_length).decode("utf-8")

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

        # Send Telegram notification for inbound messages
        # Suppress notification for sensitive messages (2FA codes, OTP, etc.)
        telegram_sent = None
        telegram_status = TELEGRAM_STATUS_NOT_APPLICABLE
        telegram_note = ""
        if direction == "inbound":
            # Resolve contact name before filtering so sender check isn't "Unknown"
            contact_info = get_contact_name(from_num)
            if not contact_info and result.get("message"):
                cached = result["message"].get("contact_name", "")
                if cached and cached != "Unknown":
                    contact_info = cached

            notification_type = classify_inbound_notification(data)
            if notification_type == "missed_call":
                line_display = get_line_name(to_num) or "Unknown"
                if contact_info:
                    sender_display = f"*{contact_info}* (`{from_num}`)"
                else:
                    sender_display = f"`{from_num}`"
                time_display = datetime.now().strftime("%I:%M %p").lstrip("0")

                tg_text = (
                    f"📞 *Missed Call*\n"
                    f"*To:* {line_display}\n"
                    f"*From:* {sender_display}\n"
                    f"*Time:* {time_display}"
                )
                telegram_sent = send_to_telegram(tg_text)
                telegram_status = TELEGRAM_STATUS_SENT if telegram_sent else TELEGRAM_STATUS_FAILED
                telegram_note = "missed call alert"
            elif notification_type == "blank_sms":
                telegram_sent = False
                telegram_status = TELEGRAM_STATUS_FILTERED
                telegram_note = "blank inbound SMS filtered"
            elif is_sensitive_message(text=text, sender=contact_info or "", contact_number=from_num):
                telegram_sent = False
                telegram_status = TELEGRAM_STATUS_FILTERED
                telegram_note = "sensitive - filtered"
                print(f"   🔒 Sensitive message filtered (not forwarding to Telegram)")
            else:
                sender_display = f"*{contact_info}* (`{from_num}`)" if contact_info else f"`{from_num}`"
                line_display = get_line_name(to_num)
                text_preview = text[:200] + "..." if len(text) > 200 else text
                to_line = f"*To:* {line_display}\n" if line_display else ""

                tg_text = (
                    f"📱 *New SMS Received*\n"
                    f"{to_line}"
                    f"*From:* {sender_display}\n"
                    f"*Message:* {text_preview}\n\n"
                    f"_Reply via Dialpad or use /sms to respond._"
                )

                telegram_sent = send_to_telegram(tg_text)
                telegram_status = TELEGRAM_STATUS_SENT if telegram_sent else TELEGRAM_STATUS_FAILED
                telegram_note = "sms notification"

        # Console logging
        print(f"[{timestamp}]")
        print(f"   📱 {direction.upper()}: {from_num}")
        if text:
            text_preview = text[:60] + "..." if len(text) > 60 else text
            print(f"   📄 \"{text_preview}\"")
        print(f"   💾 Stored: ✓")
        if direction == "inbound":
            if telegram_status == TELEGRAM_STATUS_FILTERED:
                print(f"   📨 Telegram: ⏭ ({telegram_note})")
            elif telegram_status == TELEGRAM_STATUS_SENT:
                print(f"   📨 Telegram: ✓ ({telegram_note})")
            elif telegram_status == TELEGRAM_STATUS_FAILED:
                print(f"   📨 Telegram: ✗ ({telegram_note})")
            else:
                print(f"   📨 Telegram: — ({telegram_status})")
        print()

        # Always return 200 OK (graceful degradation)
        # Webhook succeeded even if Telegram notification failed
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {
            "status": "ok",
            "stored": True,
            "telegram_sent": telegram_sent if direction == "inbound" else None,
            "telegram_status": telegram_status if direction == "inbound" else TELEGRAM_STATUS_NOT_APPLICABLE
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
    print("🚀 Dialpad SMS Webhook Server")
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
    print(f"  - Telegram: {'✓' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else '✗ (notifications disabled)'}")
    tg_ready = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    print(f"  - Call Notifications: {'✓' if tg_ready else '✗ (Telegram not fully configured)'}")
    print(f"  - Voicemail Notifications: {'✓' if tg_ready else '✗ (Telegram not fully configured)'}")
    print(f"  - JWT Verification: {'✓' if WEBHOOK_SECRET else '✗ (disabled)'}")
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
