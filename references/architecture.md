# Dialpad Architecture

```text
Dialpad OpenClaw Skill
├── SKILL.md                      # Skill trigger/instruction entrypoint
├── README.md                     # Concise setup + navigation
├── bin/                          # Supported agent-facing wrappers
│   ├── send_sms.py
│   ├── send_group_intro.py
│   ├── make_call.py
│   ├── get_call_transcript.py
│   ├── list_calls.py
│   ├── lookup_contact.py
│   ├── create_contact.py
│   ├── update_contact.py
│   ├── export_sms.py
│   ├── create_sms_webhook.py
│   ├── create_sms_draft.py
│   ├── approve_sms_draft.py
│   ├── list_sms_thread.py
│   ├── list_sms_inbox.py
│   ├── sync_sms_export.py
│   └── _dialpad_compat.py       # internal helper, not a command
├── generated/                    # Internal backend CLI used by wrappers
│   ├── dialpad
│   └── dialpad.openapi
├── scripts/                      # Operator-only operational Python tooling
│   ├── send_sms.py
│   ├── make_call.py
│   ├── list_calls.py
│   ├── call_lookup.py
│   ├── get_transcript.py
│   ├── get_ai_recap.py
│   ├── create_sms_webhook.py
│   ├── export_sms.py
│   ├── lookup_contact.py
│   ├── interaction_log.py
│   ├── log_api_client.py
│   ├── log_api_server.py
│   ├── log_outbox.py
│   ├── outbox_drift_probe.py
│   ├── call_sqlite.py
│   ├── sms_sqlite.py
│   ├── sms_storage.py
│   ├── webhook_sqlite.py
│   ├── webhook_server.py
│   ├── webhook_receiver.py
│   ├── poll_voicemails.py
│   └── parity-check.sh
├── references/                   # Deeper documentation
├── vendor/                       # Built managed deps (untracked; constructed from requirements.txt)
├── tests/
├── requirements.txt              # Pinned, hash-verified vendor/ build inputs
└── openapi.json
```

## Wrapper Execution Flow

`bin/*` is the stable agent contract. `generated/dialpad` sits behind that contract and should only be used directly by human operators for troubleshooting or regeneration work.

1. Wrapper receives task-oriented arguments.
2. Wrapper chooses the narrow backend needed for the task.
3. Most wrappers execute `generated/dialpad` with auth from env and `vendor/` (the repo's managed dependencies) on `PYTHONPATH`, so the generated CLI never depends on ambient site-packages or an `uv` on `PATH`. The SMS thread/inbox and calls wrappers use the authenticated shared interaction-log API when `DIALPAD_LOG_URL` is configured, while `bin/get_call_transcript.py` reuses proven `scripts/` HTTP/local helpers for transcripts.
4. Wrapper normalizes output for downstream workflows.

## Script Layer

Scripts in `scripts/` are retained for compatibility and operational workflows (webhooks, storage, exports, and call lookup utilities). They are no longer placed in repository root and are not the supported agent-facing interface.

`interaction_log.py` is the canonical owner boundary for SMS and calls. The
log API is a separate listener from provider webhook ingress, and
`log_outbox.py` only replays successful-send observations; it never retries a
provider send. It owns the outbox file end to end — the lock, the drain, and
the queue's own visibility — because a caller that compacted the queue itself
would be one lost entry away from silent data loss. Drain triggers live at the
command edges, which call one bounded entrypoint rather than duplicating the
policy.

## Regeneration

```bash
# Fetch latest Dialpad OpenAPI
curl -fsSL https://dash.readme.com/api/v1/api-registry/58a089fmkn6y1s3 -o openapi.json

# Generate CLI from pinned openapi2cli commit
uvx --from /tmp/openapi2cli openapi2cli generate /tmp/openapi.normalized.json --name dialpad --output generated/dialpad.openapi
```
