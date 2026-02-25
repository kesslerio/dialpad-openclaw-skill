# SMS Storage (SQLite)

Messages are stored in a single SQLite database with full-text search.

## Storage Location

```
~/.dialpad/sms.db  # Single file with messages + FTS5 index
```

## Commands

```bash
# List all SMS conversations
python3 scripts/sms_sqlite.py list

# View specific conversation thread
python3 scripts/sms_sqlite.py thread "+14155551234"

# Full-text search across all messages
python3 scripts/sms_sqlite.py search "demo"

# Show unread message summary
python3 scripts/sms_sqlite.py unread

# Statistics
python3 scripts/sms_sqlite.py stats

# Mark messages as read
python3 scripts/sms_sqlite.py read "+14155551234"

# Migrate from legacy storage
python3 scripts/sms_sqlite.py migrate
```

## Features

- **Full-text search** via FTS5 (`search "keyword"`)
- **Fast queries** with indexes on contact, timestamp, direction
- **ACID transactions** — no corruption on concurrent writes
- **Unread tracking** with per-contact counts
- **Denormalized contact stats** for instant list views

## Webhook Integration

```python
from webhook_sqlite import handle_sms_webhook, format_notification, get_inbox_summary

# Store incoming message
result = handle_sms_webhook(dialpad_payload)
notification = format_notification(result)

# Get inbox summary
summary = get_inbox_summary()
```

## Legacy JSON Storage (Deprecated)

The original JSON-based storage is still available but not recommended:

```bash
python3 scripts/sms_storage.py [list|thread|search|unread]
```

## Historical Export

Export past SMS messages as CSV using `bin/export_sms.py`.

```bash
# Export all SMS
bin/export_sms.py --output all_sms.csv

# Export by date range
bin/export_sms.py --start-date 2026-01-01 --end-date 2026-01-31 --output jan_sms.csv
```
