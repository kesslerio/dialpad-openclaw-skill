# Dialpad API & CLI Reference

Agent-facing usage should go through `bin/*` wrappers. This document also keeps the underlying CLI and API details available for manual operator troubleshooting.

## API Capabilities

### SMS
- **Endpoint:** `POST https://dialpad.com/api/v2/sms`
- **Max recipients:** 10 per request
- **Max message length:** 1600 characters
- **Rate limits:** 100-800 requests/minute (tier-dependent)

### Voice Calls
- **Endpoint:** `POST https://dialpad.com/api/v2/call`
- **Requires:** `phone_number` + `user_id`
- **Features:** Outbound calling, Text-to-Speech
- **Caller ID:** Must be assigned to your Dialpad account

## Supported Agent Surface

For agent workflows, `bin/*` wrappers are the supported contract. They provide task-focused arguments, auth bridging, and safer output behavior than the raw generated CLI.

## Generated CLI Backend (Operator Use Only)

The OpenAPI-generated CLI (`generated/dialpad`) exposes 241 endpoints. It is the backend surface used by wrappers and a manual operator troubleshooting tool, not a normal agent entrypoint.

### Wrapper Behavior Notes

- `bin/send_sms.py` resolves sender with precedence:
  - `--from`
  - `--profile work|sales`
  - `DIALPAD_DEFAULT_FROM_NUMBER`
  - `DIALPAD_DEFAULT_PROFILE`
- `--profile` maps to configured env vars:
  - `DIALPAD_PROFILE_WORK_FROM`
  - `DIALPAD_PROFILE_SALES_FROM`
- `--allow-profile-mismatch` bypasses strict profile/number binding.
- `--message-file` reads SMS text from a UTF-8 file path.
- `--message-stdin` reads SMS text from stdin.
- `--dry-run` shows resolved sender and the exact message payload without sending.
- `bin/send_group_intro.py` performs a mirrored fallback (`mode: mirrored_fallback`) by sending two separate one-to-one SMS messages because the wrapper does not guarantee a true group thread.
- `bin/list_calls.py` provides agent-safe recent call history with `--hours` or `--today`, optional missed-call filtering, and `--json` for a machine-readable envelope.
- `firstContact` includes an explicit `identityState` and raw lookup status so downstream agents can keep weak matches draft-only instead of mutating a contact record too early.

```bash
bin/send_sms.py --to "+14155550111" --message 'Hello' --profile work
printf '%s' 'The premium hardshell travel case is $499.' | bin/send_sms.py --to "+14155550111" --from "+14155201316" --message-stdin --dry-run
bin/send_group_intro.py --prospect "+14155550111" --reference "+14155559999" --confirm-share --from "+14153602954"
bin/list_calls.py --today --limit 20
bin/list_calls.py --hours 6 --missed --json
```

### Manual Operator CLI Examples

These examples are for human operators doing deep inspection or advanced troubleshooting.

### Campaign & Automation
```bash
# Bulk SMS campaigns
dialpad message bulk_messages.send --recipients '["+14155551234"]' --text "Campaign message"

# Schedule SMS for later delivery
dialpad message schedules.create --send-time "2026-02-15T09:00:00Z" --text "Reminder"

# Manage SMS templates
dialpad message templates.list
dialpad message templates.create --name "Welcome" --text "Welcome to ShapeScale!"
```

### Advanced Call Management
```bash
# Transfer live call to another user
dialpad call transfer_call --call-id "12345" --target-user-id "67890"

# Get AI-generated call summary
dialpad call ai_recap --call-id "12345"

# List call dispositions (outcomes)
dialpad dispositions list
dialpad dispositions.create --name "Demo Scheduled" --color "#00FF00"

# Initiate IVR flow
dialpad call initiate_ivr_call --phone-number "+14155551234" --ivr-id "menu_123"

# Control call recording
dialpad call recording.start --call-id "12345"
dialpad call recording.stop --call-id "12345"

# Add call labels
dialpad call put_call_labels --call-id "12345" --labels '["hot-lead", "follow-up"]'
```

### Organization Management
```bash
# User management
dialpad users users.list
dialpad users users.get --id "5765607478525952"
dialpad users users.update --id "5765607478525952" --status "away"

# Office/Department management
dialpad offices offices.list
dialpad offices offices.create --name "SF Office" --timezone "America/Los_Angeles"
dialpad departments departments.list

# Call center queues
dialpad callcenters callcenters.list
dialpad callcenters operators.list --callcenter-id "12345"

# Access control
dialpad accesscontrolpolicies accesscontrolpolicies.list
dialpad accesscontrolpolicies accesscontrolpolicies.assign --id "policy_123" --user-id "456"
```

### Contact & CRM
```bash
# Full contact CRUD (manual operator use)
dialpad contacts contacts.create --first-name "John" --last-name "Doe" --phones '["+14155551234"]'
dialpad contacts contacts.update --id "contact_123" --first-name "John" --job-title "VP"
dialpad contacts contacts.delete --id "contact_123"

# Backward-compatible wrapper
bin/create_contact.py --first-name "Jane" --last-name "Doe" --phone "+14155550123" --email "jane@example.com"
bin/update_contact.py --id "contact_123" --job-title "VP"

# Company management
dialpad companies companies.list
dialpad companies companies.create --name "Acme Corp"

# Contact upsert behavior (wrapper)
# - create_contact.py matches by phone/email for shared and/or local scope and updates on match.
# - --scope controls targets: shared, local, both, auto (owner provided => both, else shared).
# - --allow-duplicate bypasses matching and forces create.

# Contact import/export
dialpad contacts imports.create --file "contacts.csv"
```

### Analytics & Reporting
```bash
# Generate stats reports
dialpad stats stats.create --stat-type "calls" --days-ago-start 7 --days-ago-end 0
dialpad stats stats.create --stat-type "csat" --export-type "records"
dialpad stats stats.create --stat-type "dispositions" --target-id "office_123" --target-type "office"

# Get report status and download
dialpad stats stats.get --id "request_123"
```

## Webhooks

### Real-Time SMS Webhooks

Receive SMS events in real-time when messages are sent/received.

```bash
# Create a webhook subscription
bin/create_sms_webhook.py create --url "https://your-server.com/webhook/dialpad" --direction "all"

# List existing subscriptions
bin/create_sms_webhook.py list
```

**Webhook Events:**
- `sms_sent` — Outgoing SMS
- `sms_received` — Incoming SMS

**Note:** Add `message_content_export` scope to receive message text in events.

### OpenClaw Webhook Fan-Out

`scripts/webhook_server.py` can forward inbound SMS and inbound missed-call events to an OpenClaw `/hooks/agent` endpoint while still returning HTTP 200 to Dialpad when local storage/classification succeeds. Missed-call Telegram alerts use the event timestamp when available and escape dynamic Markdown fields before sending.

```bash
export OPENCLAW_GATEWAY_URL="http://127.0.0.1:18789"
export OPENCLAW_HOOKS_TOKEN="your-openclaw-hooks-token"
export OPENCLAW_HOOKS_PATH="/hooks/agent"
export OPENCLAW_HOOKS_NAME="Dialpad SMS"
export OPENCLAW_HOOKS_CALL_NAME="Dialpad Missed Call"
export OPENCLAW_HOOKS_AGENT_ID="niemand-work"
export OPENCLAW_HOOKS_SMS_ENABLED="0"
export OPENCLAW_HOOKS_CALL_ENABLED="0"
export DIALPAD_AUTO_REPLY_ENABLED="0"
export DIALPAD_SMS_APPROVAL_DB="/home/art/clawd/logs/sms_approvals.db"
export DIALPAD_SMS_APPROVAL_TOKEN="operator-only-random-token"
```

Behavior notes:

- Inbound SMS forwarding requires `OPENCLAW_HOOKS_TOKEN` and `OPENCLAW_HOOKS_SMS_ENABLED=1`
- Inbound missed-call forwarding requires `OPENCLAW_HOOKS_TOKEN` and `OPENCLAW_HOOKS_CALL_ENABLED=1`
- Leave `OPENCLAW_HOOKS_SMS_ENABLED=0` and `OPENCLAW_HOOKS_CALL_ENABLED=0` for notification-only mode
- First-contact sales-line replies create approval drafts when `DIALPAD_AUTO_REPLY_ENABLED` is truthy; they do not send SMS directly
- Low-confidence Sales SMS, including payload-only contact names, may create generic approval drafts. Low confidence suppresses personalization and CRM claims; it does not suppress the approval-gated draft by itself.
- Known contacts may create context-aware approval drafts only when `inboundContext.identityConfidence` is high and recent SMS/call continuity is no older than 14 days
- Voicemail notifications remain Telegram-only for OpenClaw fan-out, but first-contact sales-line voicemails can create SMS approval drafts when draft creation is enabled
- The local OpenClaw gateway allows explicit `niemand-work` routing and the `hook:dialpad:` session-key namespace
- For unknown inbound contacts, the hook may include a `firstContact` hint with lookup and reply-drafting signals; downstream users can map that to Attio, HubSpot, Airtable, or any other source of truth
- For eligible inbound SMS and missed calls, the hook may include `inboundContext` with identity confidence, evidence, recency, `contextDraftAllowed`, and `genericDraftAllowed` so operators can see why a draft was or was not proposed
- Identity states are preserved as data, not implied behavior: `resolved` is safe to mutate, while `ambiguous`, `not_found`, and `degraded` should stay non-mutating until the CRM/agent layer proves the identity
- The webhook server adds `autoReply` metadata for approval drafts. `sent: false` is expected until a deterministic approval command records a Dialpad success result
- CLI approval requires `DIALPAD_SMS_APPROVAL_TOKEN`; keep that token in the trusted operator surface, not in agent runtime environments
- Explicit opt-out language creates no draft, invalidates pending drafts for that customer, and emits only a human-only Telegram notice
- The repo preserves the current top-level OpenClaw hook envelope and does not claim end-to-end validation of downstream proactive enrichment behavior
- Current-turn verification still applies to `niemand-work`: stale context must not produce "Already sent" or "Already updated"; only a fresh tool result in the same turn can justify those claims.
- If your gateway listens on a different port, change `OPENCLAW_GATEWAY_URL` accordingly.

### Advanced Webhooks (CLI)
```bash
# SMS webhooks with direction filtering
dialpad subscriptions webhook_sms_event_subscription.create \
  --endpoint-id 12345 \
  --direction "inbound" \
  --event-types '["sms_received"]'

# Call event webhooks
dialpad subscriptions webhook_call_event_subscription.create \
  --endpoint-id 12345 \
  --target-type "office" \
  --target-id "67890"

# Voicemail webhooks
dialpad subscriptions webhook_voicemail_event_subscription.create \
  --endpoint-id 12345 \
  --enabled true
```

## Response Formats

### SMS Response
```json
{
  "id": "4612924117884928",
  "status": "pending",
  "message_delivery_result": "pending",
  "to_numbers": ["+14158235304"],
  "from_number": "+14155201316",
  "direction": "outbound"
}
```

### Call Response
```json
{
  "call_id": "6342343299702784",
  "status": "ringing"
}
```

## Error Handling

| Error | Meaning | Action |
|-------|---------|--------|
| `invalid_destination` | Invalid phone number | Verify E.164 format |
| `invalid_source` | Caller ID not available | Check `--from` number assignment |
| `no_route` | Cannot deliver | Check carrier/recipient |
| `user_id required` | Missing user ID | Use `--from` with known number |
