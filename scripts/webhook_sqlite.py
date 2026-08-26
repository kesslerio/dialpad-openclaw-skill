#!/usr/bin/env python3
"""
Webhook receiver for Dialpad SMS using SQLite storage
Integrates with sms_sqlite.py for persistent storage
"""

import json
import sys
from pathlib import Path

# Add skill directory to path
skill_dir = Path(__file__).parent
sys.path.insert(0, str(skill_dir))

from sms_sqlite import (
    init_db,
    normalize_provider_id,
    store_message,
    update_message_delivery,
    get_all_threads,
    get_unread,
    mark_as_read,
)

try:
    from sms_security_filter import redact_preview as _security_redact_preview
except ImportError:
    _security_redact_preview = None


def redact_preview(text, **kwargs):
    """Optional redaction: use sms_security_filter when installed, else no-op."""
    if _security_redact_preview:
        return _security_redact_preview(text, **kwargs)
    return text


def _first_nonempty(value):
    if isinstance(value, (list, tuple)):
        for item in value:
            if item is not None and str(item).strip():
                return item
        return None
    if value is not None and str(value).strip():
        return value
    return None


def _has_message_text(data: dict) -> bool:
    return bool(_first_nonempty(data.get("text")) or _first_nonempty(data.get("text_content")))


def _has_full_message_shape(data: dict) -> bool:
    direction = str(data.get("direction", "")).strip().lower()
    return (
        direction in {"inbound", "outbound"}
        and _first_nonempty(data.get("from_number")) is not None
        and _first_nonempty(data.get("to_number")) is not None
        and _has_message_text(data)
    )


def _has_delivery_signal(data: dict) -> bool:
    return (
        "message_status" in data
        or "status" in data
        or "message_delivery_result" in data
        or "delivery_result" in data
        or "event_timestamp" in data
    )


def classify_sms_webhook_event(data: dict) -> str:
    """Classify before storage/fan-out so sparse receipts cannot become messages."""
    if not isinstance(data, dict):
        return "rejected"
    if _has_full_message_shape(data):
        return "full_message"
    if _has_delivery_signal(data):
        # A non-empty body makes this a claimed message event, not a sparse
        # receipt. If participants/direction are incomplete, fail closed
        # instead of allowing a hybrid payload into either path.
        if _has_message_text(data):
            return "rejected"
        if normalize_provider_id(data.get("id") or data.get("message_id")):
            return "delivery_status"
        return "rejected"
    return "full_message"


def handle_sms_webhook(data: dict, *, event_type: str | None = None) -> dict:
    """
    Handle incoming SMS webhook from Dialpad
    Stores message in SQLite with FTS5 indexing
    """
    event_type = event_type or classify_sms_webhook_event(data)
    if event_type == "delivery_status":
        conn = None
        try:
            conn = init_db()
            receipt = update_message_delivery(conn, data)
            if receipt.get("status") == "success":
                return {
                    **receipt,
                    "status": "success",
                    "stored": True,
                    "event_type": "delivery_status",
                }
            if receipt.get("status") == "not_found":
                return {
                    **receipt,
                    "stored": False,
                    "event_type": "delivery_status",
                }
            return {
                **receipt,
                "stored": False,
                "event_type": "delivery_status",
                "error": receipt.get("reason", "receipt_rejected"),
            }
        except Exception as exc:
            return {
                "status": "error",
                "stored": False,
                "event_type": "delivery_status",
                "error": str(exc),
            }
        finally:
            if conn is not None:
                conn.close()

    # Keep the historical /store compatibility path for payloads without any
    # delivery fields, while rejecting malformed status-bearing hybrids.
    if event_type == "rejected":
        return {
            "status": "error",
            "stored": False,
            "event_type": "rejected",
            "error": "invalid_sms_event_shape",
        }

    conn = init_db()
    try:
        msg = store_message(conn, data, is_new=True)
        
        # Get updated unread count for this contact
        cursor = conn.execute(
            "SELECT unread_count, name FROM contacts WHERE phone_number = ?",
            (msg["contact_number"],)
        )
        row = cursor.fetchone()
        
        return {
            "status": "success",
            "stored": True,
            "message": {
                "id": msg.get("id"),
                "direction": msg["direction"],
                "contact_number": msg["contact_number"],
                "contact_name": msg.get("contact_name") or row["name"] if row else "Unknown",
                "preview": msg.get("text", "")[:60] + "..." if len(msg.get("text", "")) > 60 else msg.get("text", ""),
                "unread_count": row["unread_count"] if row else 0
            }
        }
    except Exception as e:
        return {
            "status": "error",
            "stored": False,
            "error": str(e)
        }
    finally:
        conn.close()


def format_notification(response: dict) -> str:
    """Format a stored message for notification"""
    if response.get("status") != "success":
        return f"❌ Failed to store message: {response.get('error', 'Unknown error')}"

    msg = response.get("message", {})
    direction_emoji = "📥" if msg.get("direction") == "inbound" else "📤"
    contact = msg.get("contact_name", "Unknown")
    number = msg.get("contact_number", "")
    preview = msg.get("preview", "")
    unread = msg.get("unread_count", 0)

    # Redact preview if message is sensitive
    preview = redact_preview(preview, sender=contact, contact_number=number)

    unread_indicator = f" ({unread} unread)" if unread > 1 else ""

    return f"{direction_emoji} **SMS from {contact}** ({number}){unread_indicator}\n> {preview}"


def get_inbox_summary() -> str:
    """Get summary of unread messages for notifications"""
    conn = init_db()
    try:
        threads = get_unread(conn)
        if not threads:
            return "📭 No unread messages"
        
        total_unread = sum(t.get("unread_count", 0) for t in threads)
        lines = [f"📬 {total_unread} unread message(s) from {len(threads)} contact(s):\n"]
        
        for t in threads[:5]:  # Show top 5
            name = t.get("name") or t["phone_number"]
            count = t["unread_count"]
            preview = (t.get("last_message_preview") or "")[:40]
            lines.append(f"  • **{name}**: {count} unread\n    > {preview}...")
        
        if len(threads) > 5:
            lines.append(f"\n  ... and {len(threads) - 5} more")
        
        return "\n".join(lines)
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/webhook_sqlite.py [test|inbox|mark-read <number>]")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "test":
        test_data = {
            "id": 99999,
            "created_date": 1769550395216,
            "event_timestamp": 1769550395917,
            "direction": "inbound",
            "from_number": "+14155559999",
            "to_number": ["+14152001316"],
            "text": "Testing the new SQLite storage with FTS5 search!",
            "text_content": "Testing the new SQLite storage with FTS5 search!",
            "contact": {"name": "Test SQLite", "id": "999"},
            "message_status": "pending",
            "mms": False
        }
        
        result = handle_sms_webhook(test_data)
        print(json.dumps(result, indent=2))
        print("\n" + format_notification(result))
    
    elif cmd == "inbox":
        print(get_inbox_summary())
    
    elif cmd == "mark-read" and len(sys.argv) >= 3:
        number = sys.argv[2]
        conn = init_db()
        try:
            count = mark_as_read(conn, number)
            print(f"✓ Marked {count} messages from {number} as read")
        finally:
            conn.close()
    
    else:
        print("Unknown command")
