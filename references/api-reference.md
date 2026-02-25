# Dialpad API & CLI Reference

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

## Generated CLI (241 Endpoints)

The OpenAPI-generated CLI (`generated/dialpad`) exposes 241 endpoints.

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
# Full contact CRUD (beyond just lookup)
dialpad contacts contacts.create --first-name "John" --last-name "Doe" --phones '["+14155551234"]'
dialpad contacts contacts.update --id "contact_123" --company-id "company_456"
dialpad contacts contacts.delete --id "contact_123"

# Backward-compatible wrapper
bin/create_contact.py --first-name "Jane" --last-name "Doe" --phone "+14155550123" --email "jane@example.com"

# Company management
dialpad companies companies.list
dialpad companies companies.create --name "Acme Corp"

# Idempotency behavior
# - Duplicate pre-check is performed by phone/email before create when either identifier is provided.
# - Use --allow-duplicate to force creation despite matches.

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
