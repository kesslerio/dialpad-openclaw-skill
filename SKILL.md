---
name: dialpad
description: Send SMS and make voice calls via Dialpad API using an OpenAPI-generated CLI with compatibility wrappers.
homepage: https://developers.dialpad.com/
---

# Dialpad Skill

Send SMS and make voice calls via the Dialpad API.

## When to Use

Use this skill to:
- Send SMS messages (individual or batch)
- Make voice calls (with TTS or custom voices)
- Manage contacts and organization settings
- Query SMS history from local SQLite database

## Available Phone Numbers

| Number | Purpose | Format |
|--------|---------|--------|
| (415) 520-1316 | Sales Team | Default for sales context |
| (415) 360-2954 | Work/Personal | Default for work context |
| (415) 991-7155 | Support SMS Only | SMS only (no voice) |

## Quick Start

**Send SMS:**
```bash
bin/send_sms.py --to "+14155551234" --message "Hello from OpenClaw!"
```

**Make Call (TTS):**
```bash
bin/make_call.py --to "+14155551234" --text "This is a call from the agent."
```

**Create Contact:**
```bash
bin/create_contact.py --first-name "Jane" --last-name "Doe" --phone "+14155550123" --email "jane@example.com"
```

**Update Contact:**
```bash
bin/update_contact.py --id "contact_123" --phone "+14155550123" --job-title "VP"
```

**Check SMS History:**
```bash
python3 sms_sqlite.py list
```

## Key Rules

1. **Format:** Always use E.164 format for numbers (e.g., `+14155551234`).
2. **Escaping:** Use single quotes for messages containing `$` to prevent shell expansion (e.g., `'Price is $10'`).
3. **Environment:** `DIALPAD_API_KEY` must be set. `ELEVENLABS_API_KEY` is optional for premium voices.
4. **Wrappers:** Use `bin/*.py` for simple tasks; use `generated/dialpad` for advanced API features.
5. **Create/Update Contact Behavior:** `bin/create_contact.py` upserts shared/local contacts by phone/email match (or forces create with `--allow-duplicate`). `bin/update_contact.py` updates by `--id` with partial fields.

## Reference Documentation

- **`references/api-reference.md`** — API endpoints, Generated CLI usage, Webhooks
- **`references/sms-storage.md`** — SQLite commands, FTS5 search, legacy storage
- **`references/voice-options.md`** — List of available TTS voices (Budget & Premium)
- **`references/architecture.md`** — System architecture, wrappers, and CLI generation

## Setup

**Required environment variable:**
```bash
export DIALPAD_API_KEY="your_key"
```

**Optional:**
```bash
export ELEVENLABS_API_KEY="your_key"
export DIALPAD_USER_MAP='{"+14153602954": "5765607478525952"}'
```
