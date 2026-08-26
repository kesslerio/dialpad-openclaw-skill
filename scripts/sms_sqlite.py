#!/usr/bin/env python3
"""
SMS SQLite Storage Manager for Dialpad with FTS5 search
Single-file database with full-text search capabilities
"""

import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

try:
    from sms_security_filter import (
        filter_messages as _security_filter_messages,
        redact_preview as _security_redact_preview,
    )
except ImportError:
    _security_filter_messages = None
    _security_redact_preview = None


def filter_messages(messages, **kwargs):
    """Optional filtering: use sms_security_filter when installed, else no-op."""
    if _security_filter_messages:
        return _security_filter_messages(messages, **kwargs)
    return messages


def redact_preview(text, **kwargs):
    """Optional redaction: use sms_security_filter when installed, else no-op."""
    if _security_redact_preview:
        return _security_redact_preview(text, **kwargs)
    return text

# Database path
def resolve_db_path() -> Path:
    return Path(os.environ.get("DIALPAD_SMS_DB", "/home/art/niemand/logs/sms.db")).expanduser()


DB_PATH = resolve_db_path()
LEGACY_THREADS_DIR = Path("/home/art/niemand/logs/sms_threads")
LEGACY_LOG = Path("/home/art/niemand/logs/dialpad_sms.jsonl")

DELIVERY_EVENT_MAX_FUTURE_MS = 5 * 60 * 1000
DELIVERY_EVENT_MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000
DELIVERY_FAILURE_RESULTS = frozenset({
    "invalid_destination",
    "invalid_source",
    "no_route",
    "not_supported",
    "rejected",
    "rejected_spam",
    "internal_error",
    "time_out",
})
DELIVERY_PENDING_STATUSES = frozenset({
    "pending",
    "queued",
    "accepted",
    "sent",
    "processing",
    "in_progress",
})
DELIVERY_POSITIVE_RESULTS = frozenset({"success", "delivered", "accepted", "sent", "ok"})
DELIVERY_PENDING_RESULTS = frozenset({"pending", "queued", "accepted", "sent"})


def normalize_provider_id(value: Any) -> str | None:
    """Return a non-empty provider identifier without guessing or coercing bools."""
    if value is None or isinstance(value, (bool, dict, list, tuple)):
        return None
    value = str(value).strip()
    return value or None


def parse_provider_event_timestamp(value: Any) -> int | None:
    """Normalize Dialpad millisecond/second or ISO timestamps to milliseconds."""
    if value is None or isinstance(value, bool):
        return None
    timestamp: int | None = None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)) or not float(value).is_integer():
            return None
        timestamp = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            timestamp = int(text)
        else:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamp = int(parsed.timestamp() * 1000)
    if timestamp is None or timestamp <= 0:
        return None
    # Dialpad uses milliseconds; accepting epoch seconds keeps the parser
    # compatible with hand-authored/replayed fixtures without changing storage.
    if timestamp < 100_000_000_000:
        timestamp *= 1000
    return timestamp


def _status_token(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    return str(value).strip().lower()


def classify_delivery_status(message_status: Any, delivery_result: Any) -> dict[str, Any]:
    """Map provider vocabulary to the shared receipt contract.

    Unknown values and contradictory terminal pairs stay unknown/conflicted;
    callers must not turn them into delivery success.
    """
    status = _status_token(message_status)
    result = _status_token(delivery_result)

    if status in {"delivered"}:
        if result in DELIVERY_FAILURE_RESULTS:
            return {"outcome": "delivery_unknown", "terminal": True, "conflict": True}
        if not result or result in DELIVERY_POSITIVE_RESULTS:
            return {"outcome": "delivered", "terminal": True, "conflict": False}
        return {"outcome": "delivery_unknown", "terminal": False, "conflict": False}

    if status in {"failed", "undelivered"}:
        if result in DELIVERY_POSITIVE_RESULTS:
            return {"outcome": "delivery_unknown", "terminal": True, "conflict": True}
        if not result or result in DELIVERY_FAILURE_RESULTS:
            return {"outcome": "undelivered", "terminal": True, "conflict": False}
        return {"outcome": "delivery_unknown", "terminal": False, "conflict": False}

    if result in DELIVERY_FAILURE_RESULTS:
        return {"outcome": "undelivered", "terminal": True, "conflict": False}

    if status in DELIVERY_PENDING_STATUSES:
        if not result or result in DELIVERY_PENDING_RESULTS:
            return {"outcome": "pending", "terminal": False, "conflict": False}
        return {"outcome": "delivery_unknown", "terminal": False, "conflict": False}
    if not status and result in DELIVERY_PENDING_RESULTS:
        return {"outcome": "pending", "terminal": False, "conflict": False}

    return {"outcome": "delivery_unknown", "terminal": False, "conflict": False}


def init_db() -> sqlite3.Connection:
    """Initialize the database with schema"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")  # serialize concurrent webhook-thread writers
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # best-effort; busy_timeout provides the serialization
    except sqlite3.OperationalError:
        pass
    conn.row_factory = sqlite3.Row
    
    # Main messages table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            dialpad_id INTEGER UNIQUE,
            contact_number TEXT NOT NULL,
            contact_name TEXT,
            direction TEXT CHECK(direction IN ('inbound', 'outbound')),
            from_number TEXT,
            to_number TEXT,
            text TEXT,
            message_status TEXT,
            delivery_result TEXT,
            mms BOOLEAN DEFAULT 0,
            mms_url TEXT,
            timestamp INTEGER,
            delivery_event_timestamp INTEGER,
            received_at TEXT DEFAULT CURRENT_TIMESTAMP,
            read BOOLEAN DEFAULT 0
        )
    """)

    # Existing production databases predate delivery receipts. This additive
    # migration is intentionally local and preserves every existing row.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "delivery_event_timestamp" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN delivery_event_timestamp INTEGER")
    
    # Contacts summary table (denormalized for fast lookups)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            phone_number TEXT PRIMARY KEY,
            name TEXT,
            message_count INTEGER DEFAULT 0,
            unread_count INTEGER DEFAULT 0,
            first_message_at INTEGER,
            last_message_at INTEGER,
            last_message_preview TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # FTS5 virtual table for full-text search
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            text,
            contact_name,
            content='messages',
            content_rowid='id'
        )
    """)
    
    # Triggers to keep FTS index in sync
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, text, contact_name)
            VALUES (new.id, new.text, new.contact_name);
        END
    """)
    
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text, contact_name)
            VALUES ('delete', old.id, old.text, old.contact_name);
        END
    """)
    
    trigger_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'messages_au'"
    ).fetchone()
    if trigger_sql and "AFTER UPDATE OF text, contact_name" not in trigger_sql[0]:
        conn.execute("DROP TRIGGER messages_au")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text, contact_name ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text, contact_name)
            VALUES ('delete', old.id, old.text, old.contact_name);
            INSERT INTO messages_fts(rowid, text, contact_name)
            VALUES (new.id, new.text, new.contact_name);
        END
    """)
    
    # Indexes for common queries
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_direction ON messages(direction)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_read ON messages(read) WHERE read = 0")
    
    conn.commit()
    return conn


def normalize_message(data: dict) -> dict:
    """Normalize webhook message to standard format"""
    direction = data.get("direction", "unknown")
    
    if direction == "inbound":
        other_number = data.get("from_number", "unknown")
        our_number = data.get("to_number", ["unknown"])[0] if data.get("to_number") else "unknown"
    else:
        to_numbers = data.get("to_number", [])
        other_number = to_numbers[0] if to_numbers else "unknown"
        our_number = data.get("from_number", "unknown")
    
    contact = data.get("contact", {})
    
    return {
        "dialpad_id": data.get("id"),
        "contact_number": other_number,
        "contact_name": contact.get("name") if contact.get("name") != other_number else None,
        "direction": direction,
        "from_number": our_number if direction == "outbound" else other_number,
        "to_number": other_number if direction == "outbound" else our_number,
        "text": data.get("text") or data.get("text_content", ""),
        "message_status": data.get("message_status") or data.get("status"),
        "delivery_result": data.get("message_delivery_result") or data.get("delivery_result"),
        "mms": data.get("mms", False),
        "mms_url": data.get("mms_url"),
        "timestamp": data.get("created_date"),
        "delivery_event_timestamp": parse_provider_event_timestamp(data.get("event_timestamp")),
    }


def store_message(conn: sqlite3.Connection, data: dict, is_new: bool = True) -> dict:
    """Store a message and update contact summary"""
    msg = normalize_message(data)
    
    cursor = conn.execute(
        """INSERT OR REPLACE INTO messages 
           (dialpad_id, contact_number, contact_name, direction, from_number, to_number,
            text, message_status, delivery_result, mms, mms_url, timestamp,
            delivery_event_timestamp, read)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (msg["dialpad_id"], msg["contact_number"], msg["contact_name"], msg["direction"],
         msg["from_number"], msg["to_number"], msg["text"], msg["message_status"],
         msg["delivery_result"], msg["mms"], msg["mms_url"], msg["timestamp"],
         msg["delivery_event_timestamp"],
         0 if (is_new and msg["direction"] == "inbound") else 1)
    )
    
    # Update contact summary
    _update_contact_summary(conn, msg["contact_number"])
    
    conn.commit()
    msg["id"] = cursor.lastrowid
    return msg


def update_message_delivery(
    conn: sqlite3.Connection,
    data: dict,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Apply one sparse receipt to an existing row, keyed only by provider ID."""
    provider_id = normalize_provider_id(data.get("id") or data.get("message_id"))
    if provider_id is None:
        return {"status": "error", "updated": False, "reason": "missing_provider_id"}

    event_timestamp = parse_provider_event_timestamp(data.get("event_timestamp"))
    if event_timestamp is None:
        return {"status": "error", "updated": False, "reason": "invalid_event_timestamp"}
    now = int(now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
    if event_timestamp > now + DELIVERY_EVENT_MAX_FUTURE_MS:
        return {"status": "error", "updated": False, "reason": "future_event_timestamp"}
    if event_timestamp < now - DELIVERY_EVENT_MAX_AGE_MS:
        return {"status": "error", "updated": False, "reason": "event_timestamp_out_of_window"}

    message_status = data.get("message_status") or data.get("status")
    delivery_result = data.get("message_delivery_result") or data.get("delivery_result")
    classification = classify_delivery_status(message_status, delivery_result)
    base = {
        "provider_id": provider_id,
        "event_timestamp": event_timestamp,
        "outcome": classification["outcome"],
        "updated": False,
    }
    if classification["conflict"]:
        return {**base, "status": "conflict", "reason": "contradictory_terminal_status"}
    if classification["outcome"] == "delivery_unknown":
        return {**base, "status": "error", "reason": "unknown_delivery_status"}

    row = conn.execute(
        "SELECT dialpad_id, message_status, delivery_result, delivery_event_timestamp "
        "FROM messages WHERE dialpad_id = ?",
        (provider_id,),
    ).fetchone()
    if row is None:
        return {**base, "status": "not_found", "reason": "provider_id_not_found"}

    existing_timestamp = parse_provider_event_timestamp(row["delivery_event_timestamp"])
    existing = classify_delivery_status(row["message_status"], row["delivery_result"])
    if existing["terminal"] and not classification["terminal"]:
        return {**base, "status": "conflict", "reason": "terminal_downgrade"}
    if existing["terminal"] and classification["terminal"] and existing["outcome"] != classification["outcome"]:
        return {**base, "status": "conflict", "reason": "terminal_outcome_changed"}
    if existing_timestamp is not None:
        if event_timestamp < existing_timestamp:
            return {**base, "status": "success", "reason": "stale"}
        if event_timestamp == existing_timestamp:
            if (
                _status_token(row["message_status"]) == _status_token(message_status)
                and _status_token(row["delivery_result"]) == _status_token(delivery_result)
            ):
                return {**base, "status": "success", "reason": "duplicate"}
            return {**base, "status": "conflict", "reason": "same_timestamp_changed_status"}

    conn.execute(
        "UPDATE messages SET message_status = ?, delivery_result = ?, "
        "delivery_event_timestamp = ? WHERE dialpad_id = ?",
        (message_status, delivery_result, event_timestamp, provider_id),
    )
    conn.commit()
    return {
        **base,
        "status": "success",
        "reason": "updated",
        "updated": True,
    }


def _update_contact_summary(conn: sqlite3.Connection, phone_number: str):
    """Update denormalized contact stats"""
    latest_name = _get_latest_contact_name(conn, phone_number)
    conn.execute("""
        INSERT INTO contacts (phone_number, name, message_count, unread_count, 
                             first_message_at, last_message_at, last_message_preview)
        SELECT 
            contact_number,
            ? as name,
            COUNT(*) as message_count,
            SUM(CASE WHEN read = 0 AND direction = 'inbound' THEN 1 ELSE 0 END) as unread_count,
            MIN(timestamp) as first_message_at,
            MAX(timestamp) as last_message_at,
            (SELECT text FROM messages 
             WHERE contact_number = ? 
             ORDER BY timestamp DESC LIMIT 1) as last_message_preview
        FROM messages
        WHERE contact_number = ?
        ON CONFLICT(phone_number) DO UPDATE SET
            name = excluded.name,
            message_count = excluded.message_count,
            unread_count = excluded.unread_count,
            first_message_at = excluded.first_message_at,
            last_message_at = excluded.last_message_at,
            last_message_preview = excluded.last_message_preview,
            updated_at = CURRENT_TIMESTAMP
    """, (latest_name, phone_number, phone_number))


def _get_latest_contact_name(conn: sqlite3.Connection, phone_number: str) -> Optional[str]:
    """Return most recent non-empty contact name seen for a phone number."""
    cursor = conn.execute(
        """
        SELECT contact_name
        FROM messages
        WHERE contact_number = ?
          AND contact_name IS NOT NULL
        ORDER BY timestamp DESC, id DESC
        """,
        (phone_number,),
    )
    for row in cursor.fetchall():
        name = row["contact_name"]
        if isinstance(name, str):
            stripped = name.strip()
            if stripped:
                return stripped
    return None


def cleanup_stale_contacts(conn: sqlite3.Connection, phone_number: Optional[str] = None) -> dict[str, int]:
    """Rebuild contact summaries from message truth and remove orphaned contact rows."""
    updated = 0
    removed = 0

    if phone_number:
        cursor = conn.execute(
            "SELECT DISTINCT contact_number FROM messages WHERE contact_number = ?",
            (phone_number,),
        )
        numbers = [row["contact_number"] for row in cursor.fetchall()]
    else:
        cursor = conn.execute(
            "SELECT DISTINCT contact_number FROM messages WHERE contact_number IS NOT NULL AND TRIM(contact_number) != ''"
        )
        numbers = [row["contact_number"] for row in cursor.fetchall()]

    for number in numbers:
        _update_contact_summary(conn, number)
        updated += 1

    if phone_number:
        orphan_cursor = conn.execute(
            "DELETE FROM contacts WHERE phone_number = ? AND phone_number NOT IN (SELECT DISTINCT contact_number FROM messages)",
            (phone_number,),
        )
    else:
        orphan_cursor = conn.execute(
            "DELETE FROM contacts WHERE phone_number NOT IN (SELECT DISTINCT contact_number FROM messages)"
        )
    removed = orphan_cursor.rowcount

    conn.commit()
    return {
        "updated": updated,
        "removed": removed,
    }


def get_thread(conn: sqlite3.Connection, phone_number: str, limit: int = 100) -> List[dict]:
    """Get conversation history for a contact"""
    cursor = conn.execute(
        """SELECT * FROM messages
           WHERE contact_number = ?
           ORDER BY timestamp ASC
           LIMIT ?""",
        (phone_number, limit)
    )
    messages = [dict(row) for row in cursor.fetchall()]
    return filter_messages(messages)


def get_all_threads(conn: sqlite3.Connection) -> List[dict]:
    """Get summary of all conversations"""
    cursor = conn.execute(
        """SELECT * FROM contacts
           ORDER BY last_message_at DESC NULLS LAST"""
    )
    threads = [dict(row) for row in cursor.fetchall()]
    for t in threads:
        t["last_message_preview"] = redact_preview(
            t.get("last_message_preview", ""),
            sender=t.get("name", ""),
            contact_number=t.get("phone_number", ""),
        )
    return threads


def search_messages(conn: sqlite3.Connection, query: str, limit: int = 20) -> List[dict]:
    """Full-text search across all messages"""
    cursor = conn.execute(
        """SELECT m.* FROM messages m
           JOIN messages_fts fts ON m.id = fts.rowid
           WHERE messages_fts MATCH ?
           ORDER BY m.timestamp DESC
           LIMIT ?""",
        (query, limit)
    )
    messages = [dict(row) for row in cursor.fetchall()]
    return filter_messages(messages)


def mark_as_read(conn: sqlite3.Connection, phone_number: str) -> int:
    """Mark all messages from a number as read"""
    cursor = conn.execute(
        "UPDATE messages SET read = 1 WHERE contact_number = ? AND read = 0",
        (phone_number,)
    )
    _update_contact_summary(conn, phone_number)
    conn.commit()
    return cursor.rowcount


def get_unread(conn: sqlite3.Connection) -> List[dict]:
    """Get all unread messages grouped by contact"""
    cursor = conn.execute(
        """SELECT * FROM contacts WHERE unread_count > 0 ORDER BY last_message_at DESC"""
    )
    threads = [dict(row) for row in cursor.fetchall()]
    for t in threads:
        t["last_message_preview"] = redact_preview(
            t.get("last_message_preview", ""),
            sender=t.get("name", ""),
            contact_number=t.get("phone_number", ""),
        )
    return threads


def migrate_from_json(conn: sqlite3.Connection):
    """Migrate from legacy JSON threaded storage"""
    if not LEGACY_THREADS_DIR.exists():
        print("No legacy thread directory found")
        return 0
    
    count = 0
    for thread_dir in LEGACY_THREADS_DIR.iterdir():
        if not thread_dir.is_dir():
            continue
        
        thread_file = thread_dir / "thread.jsonl"
        if not thread_file.exists():
            continue
        
        with open(thread_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                try:
                    msg = json.loads(line)
                    # Convert to dialpad format
                    data = {
                        "id": msg.get("dialpad_id") or msg.get("id"),
                        "created_date": msg.get("timestamp"),
                        "direction": msg.get("direction"),
                        "from_number": msg.get("from_number"),
                        "to_number": [msg.get("to_number")] if msg.get("to_number") else [],
                        "text": msg.get("text"),
                        "text_content": msg.get("text_content"),
                        "contact": {"name": msg.get("contact_name")} if msg.get("contact_name") else {},
                        "message_status": msg.get("message_status"),
                        "message_delivery_result": msg.get("delivery_result"),
                        "mms": msg.get("mms", False),
                        "mms_url": msg.get("mms_url"),
                    }
                    store_message(conn, data, is_new=False)
                    count += 1
                except Exception as e:
                    print(f"Error migrating message: {e}")
                    continue
    
    print(f"Migrated {count} messages from JSON storage")
    return count


def migrate_from_legacy_log(conn: sqlite3.Connection):
    """Migrate from the original flat file log"""
    if not LEGACY_LOG.exists():
        return 0
    
    count = 0
    with open(LEGACY_LOG, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            
            try:
                entry = json.loads(line)
                data = entry.get("data", entry)
                store_message(conn, data, is_new=False)
                count += 1
            except Exception as e:
                continue
    
    print(f"Migrated {count} messages from legacy log")
    return count


def format_timestamp(ts: Optional[int]) -> str:
    """Format millisecond timestamp to human-readable"""
    if not ts:
        return "N/A"
    try:
        dt = datetime.fromtimestamp(ts / 1000)
        return dt.strftime("%Y-%m-%d %H:%M")
    except:
        return "Invalid"


# CLI Interface
if __name__ == "__main__":
    import sys
    
    conn = init_db()
    
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/sms_sqlite.py [list|thread <number>|search <query>|unread|migrate|stats|cleanup [number]|read <number>]")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "list":
        threads = get_all_threads(conn)
        print(f"\n{'Phone Number':<20} {'Name':<30} {'Msgs':>6} {'Unread':>7} {'Last Message'}")
        print("-" * 90)
        for t in threads:
            last = format_timestamp(t.get("last_message_at"))
            name = t.get("name") or "Unknown"
            if len(name) > 28:
                name = name[:27] + "…"
            print(f"{t['phone_number']:<20} {name:<30} {t['message_count']:>6} {t['unread_count']:>7} {last}")
    
    elif cmd == "thread" and len(sys.argv) >= 3:
        number = sys.argv[2]
        messages = get_thread(conn, number)
        
        # Get contact name
        cursor = conn.execute("SELECT name FROM contacts WHERE phone_number = ?", (number,))
        row = cursor.fetchone()
        contact_name = row["name"] if row else "Unknown"
        
        print(f"\nThread with {contact_name} ({number}) - {len(messages)} messages:\n")
        for msg in messages:
            ts = format_timestamp(msg.get("timestamp"))
            direction = "← IN" if msg["direction"] == "inbound" else "→ OUT"
            read_status = "✓" if msg.get("read") else "○"
            text = msg.get("text", "No text")[:100]
            print(f"[{ts}] {direction} {read_status} {text}{'...' if len(msg.get('text', '')) > 100 else ''}")
    
    elif cmd == "search" and len(sys.argv) >= 3:
        query = sys.argv[2]
        results = search_messages(conn, query)
        print(f"\nFound {len(results)} messages matching '{query}':\n")
        
        # Group by contact
        by_contact: Dict[str, List[dict]] = {}
        for msg in results:
            num = msg["contact_number"]
            if num not in by_contact:
                by_contact[num] = []
            by_contact[num].append(msg)
        
        for number, msgs in by_contact.items():
            cursor = conn.execute("SELECT name FROM contacts WHERE phone_number = ?", (number,))
            row = cursor.fetchone()
            name = row["name"] if row else "Unknown"
            print(f"\n{name} ({number}) - {len(msgs)} matches:")
            for msg in msgs[:3]:
                ts = format_timestamp(msg.get("timestamp"))
                direction = "←" if msg["direction"] == "inbound" else "→"
                text = msg.get("text", "")[:70]
                print(f"  [{ts}] {direction} {text}...")
    
    elif cmd == "unread":
        threads = get_unread(conn)
        if not threads:
            print("\nNo unread messages.")
        else:
            print(f"\n{len(threads)} conversations with unread messages:\n")
            for t in threads:
                preview = t.get("last_message_preview", "")[:50]
                print(f"  {t.get('name') or t['phone_number']}: {t['unread_count']} unread")
                print(f"    > {preview}...\n")
    
    elif cmd == "migrate":
        migrate_from_legacy_log(conn)
        migrate_from_json(conn)
    
    elif cmd == "stats":
        cursor = conn.execute("SELECT COUNT(*) as total FROM messages")
        total = cursor.fetchone()["total"]
        
        cursor = conn.execute("SELECT COUNT(*) as unread FROM messages WHERE read = 0 AND direction = 'inbound'")
        unread = cursor.fetchone()["unread"]
        
        cursor = conn.execute("SELECT COUNT(DISTINCT contact_number) as contacts FROM messages")
        contacts = cursor.fetchone()["contacts"]
        
        cursor = conn.execute("SELECT COUNT(*) as inbound FROM messages WHERE direction = 'inbound'")
        inbound = cursor.fetchone()["inbound"]
        
        cursor = conn.execute("SELECT COUNT(*) as outbound FROM messages WHERE direction = 'outbound'")
        outbound = cursor.fetchone()["outbound"]
        
        print(f"\nSMS Storage Statistics:")
        print(f"  Total messages: {total}")
        print(f"  Unique contacts: {contacts}")
        print(f"  Inbound: {inbound} | Outbound: {outbound}")
        print(f"  Unread: {unread}")
        print(f"  Database size: {DB_PATH.stat().st_size / 1024:.1f} KB")

    elif cmd == "cleanup":
        target = sys.argv[2] if len(sys.argv) >= 3 else None
        result = cleanup_stale_contacts(conn, phone_number=target)
        scope = target or "all contacts"
        print(f"Reconciled cache for {scope}")
        print(f"  Updated contact summaries: {result['updated']}")
        print(f"  Removed orphan contact rows: {result['removed']}")
    
    elif cmd == "read" and len(sys.argv) >= 3:
        number = sys.argv[2]
        count = mark_as_read(conn, number)
        print(f"Marked {count} messages from {number} as read")
    
    else:
        print("Unknown command. Usage: python3 scripts/sms_sqlite.py [list|thread <number>|search <query>|unread|migrate|stats|cleanup [number]|read <number>]")
    
    conn.close()
