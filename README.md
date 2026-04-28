# Dialpad OpenClaw Skill

Dialpad messaging and calling skill for OpenClaw with task-focused wrappers, webhook handling, and local SMS history storage.

## What This Repo Contains

- `SKILL.md` for skill loading and agent-safe usage guidance.
- `bin/` wrappers for the supported agent-facing command surface.
- `generated/` internal OpenAPI-generated backend CLI used by wrappers and manual troubleshooting.
- `scripts/` operational Python scripts for webhook, storage, and maintenance workflows.
- `references/` deeper API/architecture/storage/voice docs.

## Quick Start

```bash
# Clone
git clone https://github.com/kesslerio/dialpad-openclaw-skill.git
cd dialpad-openclaw-skill

# Required auth (canonical)
export DIALPAD_API_KEY="your-api-key"

# Optional premium TTS
export ELEVENLABS_API_KEY="your-elevenlabs-key"
```

## Common Commands

Use `bin/` wrappers for all normal agent work. They are the stable, supported command contract.

```bash
# Send SMS (recommended: explicit sender)
bin/send_sms.py --to "+14155551234" --from "+14155201316" --message 'Hello from OpenClaw'

# Send SMS with sender profile
bin/send_sms.py --to "+14155551234" --message 'Hello' --profile work

# Send SMS with shell-sensitive pricing text
printf '%s' 'The premium hardshell travel case is $499.' | \
  bin/send_sms.py --to "+14155551234" --from "+14155201316" --message-stdin --dry-run

# Create and approve an exact-text SMS draft
bin/create_sms_draft.py --thread-key "hook:dialpad:sms:conv-123" --to "+14155551234" --from "+14155201316" --message 'Draft text' --json
bin/approve_sms_draft.py smsdraft_abc123 --actor-id "telegram-user-123" --actor-username "operator" --approval-token "$DIALPAD_SMS_APPROVAL_TOKEN" --json

# Make a call with TTS
bin/make_call.py --to "+14155551234" --text "This is a test call."

# List recent calls
bin/list_calls.py --today --limit 20
bin/list_calls.py --hours 6 --missed --json

# Group intro (mirrored fallback)
bin/send_group_intro.py --prospect "+14155550111" --reference "+14155550999" --confirm-share --from "+14153602954"

# Create/update contacts
bin/create_contact.py --first-name "Jane" --last-name "Doe" --phone "+14155550123" --email "jane@example.com"
bin/update_contact.py --id "contact_123" --job-title "Director"
```

## Webhooks to OpenClaw

```bash
# Optional webhook auth validation
export DIALPAD_WEBHOOK_SECRET="your-dialpad-webhook-secret"

# OpenClaw hook destination
export OPENCLAW_GATEWAY_URL="http://127.0.0.1:18789"
export OPENCLAW_HOOKS_TOKEN="your-openclaw-hooks-token"
export OPENCLAW_HOOKS_PATH="/hooks/agent"
export OPENCLAW_HOOKS_NAME="Dialpad SMS"
export OPENCLAW_HOOKS_CALL_NAME="Dialpad Missed Call"
export OPENCLAW_HOOKS_AGENT_ID="niemand-work"

# Optional per-event hook controls (disabled by default)
export OPENCLAW_HOOKS_SMS_ENABLED="1"
export OPENCLAW_HOOKS_CALL_ENABLED="1"

# Optional sales-line first-contact auto-replies (disabled by default)
export DIALPAD_AUTO_REPLY_ENABLED="1"
export DIALPAD_SMS_APPROVAL_DB="/home/art/clawd/logs/sms_approvals.db"
export DIALPAD_SMS_APPROVAL_TOKEN="operator-only-random-token"

# Optional Telegram inline approval buttons (disabled by default)
export DIALPAD_TELEGRAM_APPROVAL_BUTTONS_ENABLED="0"
export TELEGRAM_WEBHOOK_SECRET="telegram-secret-token"
```

When `OPENCLAW_HOOKS_TOKEN` is configured, inbound SMS and inbound missed-call events are only forwarded to OpenClaw when the matching event flag is explicitly enabled. Leave `OPENCLAW_HOOKS_SMS_ENABLED=0` and `OPENCLAW_HOOKS_CALL_ENABLED=0` for notification-only mode.
If your gateway listens on a different port, change `OPENCLAW_GATEWAY_URL` accordingly.
The local gateway allows explicit `niemand-work` routing and `hook:dialpad:` session keys.
For first-time or unknown inbound contacts, the payload also carries a `firstContact` hint with an explicit `identityState` plus lookup details so OpenClaw can enrich identity, look up business context, draft a reply, and suggest Dialpad contact sync when the match is clear.
That pattern is CRM-agnostic: Attio is one example, but the same setup works with HubSpot, Pipedrive, Airtable, a spreadsheet, or a custom directory service downstream.
Current-turn verification still applies: "Already sent" and "Already updated" are only valid after a fresh current-turn tool result, not from stale session memory.
For identity work, treat `resolved` as the only state that is safe to mutate automatically; `not_found`, `ambiguous`, and `degraded` stay draft-only until the CRM layer proves otherwise.
When `DIALPAD_AUTO_REPLY_ENABLED` is set, first-contact inbound events to the sales line `(415) 520-1316` create a short exact-text approval draft instead of sending SMS directly. Missed calls get a "sorry we missed you" draft variant; SMS and voicemail get a "we'll be in touch shortly" draft variant. Explicit opt-out language creates no draft, invalidates pending drafts for that customer, and sends only a human-only Telegram notice.
CLI approval is disabled unless `DIALPAD_SMS_APPROVAL_TOKEN` is configured and supplied to `bin/approve_sms_draft.py`; keep that token out of agent runtime environments.

Telegram inline approval buttons are optional. Before setting `DIALPAD_TELEGRAM_APPROVAL_BUTTONS_ENABLED=1`, run a bot-delivery preflight: check Telegram `getWebhookInfo` for the configured bot and confirm no OpenClaw runtime or operator process is already consuming that bot with `getUpdates` polling or another webhook. Telegram webhooks and `getUpdates` polling are mutually exclusive for the same bot. If another owner exists, route callback handling through that owner or use a separate Dialpad approval bot.

When enabled, Telegram buttons call `POST /webhook/telegram` with callback queries. Configure Telegram with `allowed_updates=["callback_query"]`, `drop_pending_updates=true`, and `secret_token="$TELEGRAM_WEBHOOK_SECRET"` so requests include `X-Telegram-Bot-Api-Secret-Token`. The webhook validates that header, the configured Telegram chat id, and the human actor before dispatching to the deterministic SMS approval ledger. The button payload contains only a short draft id/action reference; it never contains the draft text or `DIALPAD_SMS_APPROVAL_TOKEN`.

Live smoke test for buttons: send a harmless fake/test inbound SMS, verify the Telegram review message shows inline buttons, click `Reject`, and confirm the draft is rejected/stale in `sms_approvals.db`. Do not click approve on smoke tests unless the destination is a controlled non-customer number.

Create/list webhook subscriptions:

```bash
bin/create_sms_webhook.py create --url "https://your-server.com/webhook/dialpad" --direction "all"
bin/create_sms_webhook.py list
```

Notes:

- `/webhook/dialpad` handles SMS storage plus optional OpenClaw/Telegram fan-out
- `/webhook/telegram` handles Telegram inline approval button callbacks; it requires `X-Telegram-Bot-Api-Secret-Token` and the configured Telegram chat id
- `/webhook/dialpad-call` handles missed-call Telegram alerts using the event timestamp when available, with dynamic Markdown escaping, plus optional OpenClaw hook forwarding
- `/webhook/dialpad-voicemail` sends Telegram alerts and can create first-contact sales-line SMS approval drafts, but does not send SMS directly
- This repo validates hook request shape, gating, and graceful degradation only. It does not validate downstream OpenClaw proactive enrichment behavior

## Operational Tools

These commands are for manual operator workflows, storage inspection, and maintenance. They are not the supported agent-facing interface.

```bash
# SMS SQLite history
python3 scripts/sms_sqlite.py list

# SMS approval drafts
python3 scripts/create_sms_draft.py --thread-key "manual:test" --to "+14155551234" --from "+14155201316" --message 'Draft text' --json
python3 scripts/approve_sms_draft.py smsdraft_abc123 --actor-id "telegram-user-123" --approval-token "$DIALPAD_SMS_APPROVAL_TOKEN" --json

# Deep webhook/storage operations
python3 scripts/webhook_server.py
python3 scripts/sms_storage.py list
```

## Manual Troubleshooting

`generated/dialpad` is an internal backend surface for the wrappers. Use it directly only for manual operator troubleshooting, API inspection, or regeneration work.

```bash
export DIALPAD_TOKEN="${DIALPAD_TOKEN:-$DIALPAD_API_KEY}"
generated/dialpad --api-key "$DIALPAD_API_KEY" company company.get >/dev/null
```

## Repository Layout

```text
dialpad-openclaw-skill/
├── SKILL.md
├── README.md
├── bin/
│   ├── list_calls.py
├── generated/
├── scripts/
├── references/
├── tests/
└── LICENSE
```

## Reference Docs

- `references/api-reference.md`
- `references/architecture.md`
- `references/openclaw-integration.md`
- `references/sms-storage.md`
- `references/voice-options.md`

## Notes

- Root Python entrypoints were consolidated into `scripts/`.
- If you previously used `python3 <root-script>.py`, switch to `python3 scripts/<script>.py`.
- Agents should use `bin/*` wrappers for normal work. Treat `generated/dialpad` as operator-only troubleshooting infrastructure.
- For messages containing `$` or other shell-sensitive text, prefer `--message-file` or `--message-stdin`. If you use inline `--message`, single-quote it.
- `bin/send_sms.py --dry-run` now prints the exact message preview so pricing/shell corruption is visible before send.
