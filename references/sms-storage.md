# SMS Storage (SQLite)

Messages are stored in a single SQLite database with full-text search.

## Storage Location

```
~/.dialpad/sms.db  # Single file with messages + FTS5 index
```

## Shared Interaction Log

For cross-host SMS and call memory, use the `InteractionLog` facade. It keeps
SMS in `DIALPAD_SMS_DB` and reuses the existing calls database in
`DIALPAD_CALLS_DB`; it never creates a calls table inside the SMS database.
The theshop canonical paths are:

```text
DIALPAD_SMS_DB=/home/art/niemand/logs/sms.db
DIALPAD_CALLS_DB=/home/art/niemand/logs/calls.db
```

The private, Tailscale-only log API listens on `100.85.254.62:18887` by
default. Clients use:

```text
DIALPAD_LOG_URL=http://100.85.254.62:18887
DIALPAD_LOG_TOKEN=<operator-managed-secret>
DIALPAD_LOG_OUTBOX=~/.dialpad/log-outbox.jsonl
```

`DIALPAD_LOG_TOKEN` is required by the API and must live in the operator
secret environment, not in the repository. The API provides authenticated
read access to `/v1/sms/thread`, `/v1/sms/inbox`, and `/v1/calls`, plus
`POST /v1/sms/record` for message observations. It intentionally does not
provide an agent-facing call-record endpoint. The provider webhook remains a
separate service on port `8888`.

Message identity uses the Dialpad provider id when available. For observations
without an id, the facade uses a constrained SHA-256 fallback over direction,
participants, a one-minute timestamp bucket, exact body when known, and MMS
shape. Empty or unknown body observations can enrich metadata but never erase
a known body; provider id upgrades preserve the existing row and provenance.

When `DIALPAD_LOG_URL` is set, `bin/list_sms_thread.py`,
`bin/list_sms_inbox.py`, and `bin/list_calls.py` read shared history by
default. Use `bin/list_calls.py --live` for an explicit live Dialpad query,
or `--local` for the legacy local SQLite read. There is no implicit mixing of
provider and shared-log results.

After a successful `bin/send_sms.py` provider send, the wrapper records the
exact outbound observation. If the log API is unavailable, it writes a
record-only JSONL item to `DIALPAD_LOG_OUTBOX`; it never retries the provider
send. Replay pending observations after the log service is healthy:

```bash
python3 scripts/log_outbox.py replay --json
```

Replay only records observations and does not send SMS or place calls.

### theshop deployment

Install the example user unit from the skill root on theshop, populate the
token through the referenced secrets file, and then manage only the log API
unit:

```bash
install -D -m 0644 systemd/dialpad-log-api.service \
  ~/.config/systemd/user/dialpad-log-api.service
systemctl --user daemon-reload
systemctl --user enable --now dialpad-log-api.service
systemctl --user status dialpad-log-api.service
```

The unit pins `DIALPAD_LOG_BIND=100.85.254.62`,
`DIALPAD_LOG_PORT=18887`, and the canonical SMS/calls paths. Set
`DIALPAD_LOG_URL` and the same token in each client environment. Do not bind
the log API to `0.0.0.0`, expose it through a public tunnel, or put the
token in `.env` files committed to git. These commands are deployment notes;
they were not run from this sandbox.

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

# Reconcile stale contact cache for all contacts
python3 scripts/sms_sqlite.py cleanup

# Reconcile stale contact cache for one number
python3 scripts/sms_sqlite.py cleanup "+14155551234"

# Migrate from legacy storage
python3 scripts/sms_sqlite.py migrate
```

## Features

- **Full-text search** via FTS5 (`search "keyword"`)
- **Fast queries** with indexes on contact, timestamp, direction
- **ACID transactions** — no corruption on concurrent writes
- **Unread tracking** with per-contact counts
- **Denormalized contact stats** for instant list views
- **Cache reconciliation** to refresh stale contact-name mappings from latest message truth

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
