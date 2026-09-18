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
send.

The outbox drains itself. Every `bin/send_sms.py` send and every
`bin/list_sms_inbox.py`, `bin/list_sms_thread.py`, and `bin/list_calls.py`
read drains it after committing its own output, bounded by an entry cap and a
wall-clock budget so an unreachable log cannot delay anything the caller is
waiting on:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DIALPAD_LOG_OUTBOX_DRAIN_LIMIT` | `5` | Maximum observations recorded per drain. |
| `DIALPAD_LOG_OUTBOX_DRAIN_SECONDS` | `2.0` | Wall-clock budget, checked between entries. One in-flight request may overrun it by up to `DIALPAD_LOG_TIMEOUT`. |
| `DIALPAD_LOG_OUTBOX_LOCK_SECONDS` | `2.0` | How long a drain waits for the outbox lock before declining. A declined drain reports `skipped`, never an empty queue. |
| `DIALPAD_LOG_OUTBOX_ALERT_AFTER_SECONDS` | `21600` | Oldest-entry age at which a breach is reported. |
| `DIALPAD_LOG_OUTBOX_NOTIFY_CMD` | unset | Command taking the alert as one argument, exiting 0 only on a confirmed send. Unset means no alert travels. |

Manual replay still works, and is what to reach for after repairing the log
service or when a large backlog needs draining in one go:

```bash
python3 scripts/log_outbox.py replay --json
```

Replay only records observations and does not send SMS or place calls.

Three siblings sit beside the outbox, each derived from it; none is a second
source of truth about what is pending:

- `log-outbox-quarantine.jsonl` — lines the reader could not parse. One poison
  line costs one line and no longer stalls the whole lane.
- `log-outbox-drains.jsonl` — one append-only record per drain run, carrying its
  own timestamp with attempted/succeeded/failed counts, remaining depth, and the
  oldest age. This is what makes a host with no scheduler detectable to anything
  that does have a clock; nothing on that host notices its own silence.
- `log-outbox-alert.json` — the breach marker. Written undelivered before an
  alert is attempted, and flipped to delivered only on a confirmed send, so a
  run that dies mid-alert retries instead of swallowing the breach.

Grok Bot has no `cron` and no systemd user scheduler, which is why the drain
rides on skill use. The limit is worth stating plainly: an outbox on a host
nobody uses stays put, and the run record is how long that has been true.

### Runtime copies

Three copies of this skill run in the world and only one is a repository. They
have drifted in both directions, so no recursive copy is safe in either
direction. `references/outbox-runtime-files.txt` names the runtime files that
carry outbox behavior, and the probe compares one copy against this repo by
content hash, restricted to that reviewed list:

```bash
python3 scripts/outbox_drift_probe.py \
  --manifest references/outbox-runtime-files.txt \
  --target ~/.ai/skills/dialpad
```

Exit `0` means every manifest entry matches. Run it before and after applying a
fix, so delivery leaves a checkable record rather than a hope.

`docs/reference/runtime-copies.md` holds the per-copy delivery record, including
which copy was deliberately left alone and why.

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
