---
name: dialpad
description: Send SMS and make voice calls via Dialpad API using task-focused wrappers backed by an OpenAPI-generated CLI.
homepage: https://developers.dialpad.com/
---

# Dialpad Skill

Send SMS and make voice calls via the Dialpad API.

## When to Use

Use this skill to:
- Send SMS messages (individual or batch)
- Make voice calls (with TTS or custom voices)
- Manage contacts and organization settings
- Inspect SMS history through operator tooling when needed

## Available Phone Numbers

| Number | Purpose | Format |
|--------|---------|--------|
| (415) 520-1316 | Sales Team | Default for sales context |
| (415) 360-2954 | Work/Personal | Default for work context |
| (415) 991-7155 | Support SMS Only | SMS only (no voice) |

## Quick Start

**Send SMS (explicit sender recommended):**
```bash
bin/send_sms.py --to "+14155551234" --from "+14155201316" --message 'Hello from OpenClaw!'
```

**Create/approve an SMS draft (human approval path):**
```bash
bin/create_sms_draft.py --thread-key "manual:thread" --to "+14155551234" --from "+14155201316" --message 'Exact draft text' --json
bin/approve_sms_draft.py smsdraft_abc123 --actor-id "telegram-user-123" --actor-username "operator" --approval-token "$DIALPAD_SMS_APPROVAL_TOKEN" --json
```

**Group Intro (mirrored fallback):**
```bash
bin/send_group_intro.py --prospect "+14155550111" --reference "+14155559999" --confirm-share --from "+14153602954"
```

**Make Call (TTS):**
```bash
bin/make_call.py --to "+14155551234" --text "This is a call from the agent."
```

**List Recent Calls:**
```bash
bin/list_calls.py --today --limit 20
bin/list_calls.py --hours 6 --missed --json
```

**Create Contact:**
```bash
bin/create_contact.py --first-name "Jane" --last-name "Doe" --phone "+14155550123" --email "jane@example.com"
```

**Update Contact:**
```bash
bin/update_contact.py --id "contact_123" --phone "+14155550123" --job-title "VP"
```

## Key Rules

1. **Format:** Always use E.164 format for numbers (e.g., `+14155551234`).
2. **Escaping:** Use single quotes for inline `--message` values containing `$` to prevent shell expansion (e.g., `'Price is $10'`).
3. **Safer message input:** Prefer `--message-file` or `--message-stdin` for pricing text, multi-line copy, or anything shell-sensitive.
4. **Supported agent interface:** use `bin/*.py` wrappers for normal work. They are the stable command contract for agents.
5. **Operator-only surfaces:** `generated/dialpad` and `scripts/*` are for manual troubleshooting, storage inspection, or operational maintenance, not normal agent task execution.
6. **Auth canonical source:** `DIALPAD_API_KEY` is canonical. `DIALPAD_TOKEN` is only needed for manual generated CLI troubleshooting.
   - Operator example: `export DIALPAD_TOKEN="${DIALPAD_TOKEN:-$DIALPAD_API_KEY}"`
7. **SMS sender safety:** `--from` and `--profile work|sales` are supported. Prefer explicit `--from` for deterministic routing.
   - `--profile` maps to configured env vars:
     - work: `DIALPAD_PROFILE_WORK_FROM`
     - sales: `DIALPAD_PROFILE_SALES_FROM`
   - default fallback order: `DIALPAD_DEFAULT_FROM_NUMBER`, then `DIALPAD_DEFAULT_PROFILE`
   - `--allow-profile-mismatch` permits explicit/profile mismatches when intentional
   - `--dry-run` prints sender resolution and the exact message/request preview without an API call
8. **Group intro:** `bin/send_group_intro.py` mirrors intro messages as two one-to-one SMS sends (`mirrored_fallback`) because true group threads are unsupported via this wrapper.
9. **Call history:** `bin/list_calls.py` is the supported call-history command for agents. Use `--json` when downstream automation needs a deterministic response envelope.
10. **Create/Update Contact Behavior:** `bin/create_contact.py` upserts shared/local contacts by phone/email match (or forces create with `--allow-duplicate`). `bin/update_contact.py` updates by `--id` with partial fields.
11. **Current-turn verification:** "Already sent" and "Already updated" are only valid after a fresh current-turn tool result, not from stale session memory. If the current turn has not verified the action yet, say that plainly and run the tool now.
12. **Identity guardrail:** For first-contact work, soft signals like first name, area code, industry, or job title are not enough to merge or update a contact. Keep uncertain identity `draft-only` and let the CRM layer prove the match before mutating anything.
13. **Inbound automation guardrail:** Dialpad inbound hooks may create SMS approval drafts, but they must not send customer SMS directly. Use `bin/approve_sms_draft.py` only with a real human actor id and an operator-only `DIALPAD_SMS_APPROVAL_TOKEN`; agent/bot actors are rejected by the approval ledger.
14. **Opt-out guardrail:** Explicit opt-out language is a hard stop. Do not create override drafts or send follow-ups on those threads unless a human operator handles the conversation outside automation.

## Reference Documentation

- **`references/api-reference.md`** — Wrapper behavior, operator CLI reference, Webhooks
- **`references/openclaw-integration.md`** — End-to-end setup guide for wiring this repo's webhook server to OpenClaw with human approval defaults
- **`references/sms-storage.md`** — SQLite commands, FTS5 search, legacy storage
- **`references/voice-options.md`** — List of available TTS voices (Budget & Premium)
- **`references/architecture.md`** — System architecture, wrappers, and CLI generation

## Operational Tools

Use these only for manual operator workflows, storage inspection, and maintenance:

```bash
python3 scripts/sms_sqlite.py list
python3 scripts/webhook_server.py
```

## Setup

**Required environment variable:**
```bash
export DIALPAD_API_KEY="your_key"
```

**Operator auth bridge for manual generated CLI troubleshooting:**
```bash
export DIALPAD_TOKEN="${DIALPAD_TOKEN:-$DIALPAD_API_KEY}"
```

**Optional:**
```bash
export ELEVENLABS_API_KEY="your_key"
export DIALPAD_USER_MAP='{"+14153602954": "5765607478525952"}'
export DIALPAD_PROFILE_WORK_FROM="+14153602954"
export DIALPAD_PROFILE_SALES_FROM="+14155201316"
export DIALPAD_DEFAULT_PROFILE="work"
export DIALPAD_DEFAULT_FROM_NUMBER="+14155201316"
```
