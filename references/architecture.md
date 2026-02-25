# Dialpad Architecture

```text
Dialpad OpenClaw Skill
├── SKILL.md                      # Skill trigger/instruction entrypoint
├── README.md                     # Concise setup + navigation
├── bin/                          # Preferred user-facing wrappers
│   ├── send_sms.py
│   ├── send_group_intro.py
│   ├── make_call.py
│   ├── lookup_contact.py
│   ├── create_contact.py
│   ├── update_contact.py
│   ├── export_sms.py
│   ├── create_sms_webhook.py
│   └── _dialpad_compat.py
├── generated/                    # OpenAPI-generated CLI
│   ├── dialpad
│   └── dialpad.openapi
├── scripts/                      # Operational/legacy Python tooling
│   ├── send_sms.py
│   ├── make_call.py
│   ├── list_calls.py
│   ├── call_lookup.py
│   ├── get_transcript.py
│   ├── get_ai_recap.py
│   ├── create_sms_webhook.py
│   ├── export_sms.py
│   ├── lookup_contact.py
│   ├── sms_sqlite.py
│   ├── sms_storage.py
│   ├── webhook_sqlite.py
│   ├── webhook_server.py
│   ├── webhook_receiver.py
│   ├── poll_voicemails.py
│   └── parity-check.sh
├── references/                   # Deeper documentation
├── tests/
└── openapi.json
```

## Wrapper to Generated CLI Flow

1. Wrapper receives task-oriented arguments.
2. Wrapper transforms arguments to Dialpad CLI payloads.
3. Wrapper executes `generated/dialpad` with auth from env.
4. Wrapper normalizes output for downstream workflows.

## Script Layer

Scripts in `scripts/` are retained for compatibility and operational workflows (webhooks, storage, exports, and call lookup utilities). They are no longer placed in repository root.

## Regeneration

```bash
# Fetch latest Dialpad OpenAPI
curl -fsSL https://dash.readme.com/api/v1/api-registry/58a089fmkn6y1s3 -o openapi.json

# Generate CLI from pinned openapi2cli commit
uvx --from /tmp/openapi2cli openapi2cli generate /tmp/openapi.normalized.json --name dialpad --output generated/dialpad.openapi
```
