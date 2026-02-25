# Dialpad OpenClaw Skill

Dialpad messaging and calling skill for OpenClaw with lightweight wrappers, webhook handling, and local SMS history storage.

## What This Repo Contains

- `SKILL.md` for skill loading and usage guidance.
- `bin/` wrappers for stable task-focused commands.
- `generated/` OpenAPI-generated Dialpad CLI surface.
- `scripts/` operational Python scripts (legacy entrypoints + webhook/storage tooling).
- `references/` deeper API/architecture/storage/voice docs.

## Quick Start

```bash
# Clone
git clone https://github.com/kesslerio/dialpad-openclaw-skill.git
cd dialpad-openclaw-skill

# Required auth
export DIALPAD_API_KEY="your-api-key"

# Optional premium TTS
export ELEVENLABS_API_KEY="your-elevenlabs-key"
```

## Common Commands

Prefer wrappers in `bin/` for day-to-day usage.

```bash
# Send SMS
bin/send_sms.py --to "+14155551234" --message "Hello from OpenClaw"

# Send SMS with sender profile
bin/send_sms.py --to "+14155551234" --message "Hello" --profile work

# Make a call with TTS
bin/make_call.py --to "+14155551234" --text "This is a test call."

# Group intro (mirrored fallback)
bin/send_group_intro.py --prospect "+14155550111" --reference "+14155550999" --confirm-share --from "+14153602954"

# Create/update contacts
bin/create_contact.py --first-name "Jane" --last-name "Doe" --phone "+14155550123" --email "jane@example.com"
bin/update_contact.py --id "contact_123" --job-title "Director"

# SMS SQLite history (script moved under scripts/)
python3 scripts/sms_sqlite.py list
```

## Webhooks to OpenClaw

```bash
# Optional webhook auth validation
export DIALPAD_WEBHOOK_SECRET="your-dialpad-webhook-secret"

# OpenClaw hook destination
export OPENCLAW_GATEWAY_URL="http://127.0.0.1:8080"
export OPENCLAW_HOOKS_TOKEN="your-openclaw-hooks-token"
export OPENCLAW_HOOKS_PATH="/hooks/agent"
export OPENCLAW_HOOKS_NAME="Dialpad SMS"
```

Create/list webhook subscriptions:

```bash
python3 scripts/create_sms_webhook.py create --url "https://your-server.com/webhook/dialpad" --direction "all"
python3 scripts/create_sms_webhook.py list
```

## Repository Layout

```text
dialpad-openclaw-skill/
├── SKILL.md
├── README.md
├── bin/
├── generated/
├── scripts/
├── references/
├── tests/
└── LICENSE
```

## Reference Docs

- `references/api-reference.md`
- `references/architecture.md`
- `references/sms-storage.md`
- `references/voice-options.md`

## Notes

- Root Python entrypoints were consolidated into `scripts/`.
- If you previously used `python3 <root-script>.py`, switch to `python3 scripts/<script>.py`.
