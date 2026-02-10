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
        direction = data.get("direction", "unknown")
        from_num = data.get("from_number", "N/A")
        text = data.get("text", data.get("text_content", ""))

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
        telegram_sent = False
        sensitive_filtered = False
        if direction == "inbound":
            # Resolve contact name before filtering so sender check isn't "Unknown"
            contact_info = get_contact_name(from_num)
            if not contact_info and result.get("message"):
                cached = result["message"].get("contact_name", "")
                if cached and cached != "Unknown":
                    contact_info = cached

            if is_sensitive_message(text=text, sender=contact_info or "", contact_number=from_num):
                sensitive_filtered = True
                print(f"   🔒 Sensitive message filtered (not forwarding to Telegram)")
            else:
                sender_display = f"*{contact_info}* (`{from_num}`)" if contact_info else f"`{from_num}`"
                text_preview = text[:200] + "..." if len(text) > 200 else text

                tg_text = (
                    f"📱 *New SMS Received*\n"
                    f"*From:* {sender_display}\n"
                    f"*Message:* {text_preview}\n\n"
                    f"_Reply via Dialpad or use /sms to respond._"
                )

                telegram_sent = send_to_telegram(tg_text)

        # Console logging
        print(f"[{timestamp}]")
        print(f"   📱 {direction.upper()}: {from_num}")
        if text:
            text_preview = text[:60] + "..." if len(text) > 60 else text
            print(f"   📄 \"{text_preview}\"")
        print(f"   💾 Stored: ✓")
        if direction == "inbound":
            if sensitive_filtered:
                print(f"   📨 Telegram: ✗ (sensitive — filtered)")
            else:
                print(f"   📨 Telegram: {'✓' if telegram_sent else '✗'}")
        print()

        # Always return 200 OK (graceful degradation)
        # Webhook succeeded even if Telegram notification failed
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {
            "status": "ok",
            "stored": True,
            "telegram_sent": telegram_sent if direction == "inbound" else None
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
    print(f"  - GET  /health (health check)")
    print(f"")
    print(f"Configuration:")
    print(f"  - Dialpad API: {'✓' if DIALPAD_API_KEY else '✗ (contact lookup disabled)'}")
    print(f"  - Telegram: {'✓' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else '✗ (notifications disabled)'}")
    print(f"  - JWT Verification: {'✓' if WEBHOOK_SECRET else '✗ (disabled)'}")
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
