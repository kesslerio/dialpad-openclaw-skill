# Dialpad Architecture

```
Dialpad SMS Skill
├── bin/                          # Backward-compatible wrappers
│   ├── send_sms.py              # Send SMS (wrapper → dialpad sms send)
│   ├── make_call.py             # Make voice calls (wrapper → dialpad call make)
│   ├── lookup_contact.py        # Contact lookup (wrapper → dialpad contact lookup)
│   ├── create_contact.py        # Contact create/upsert (wrapper → dialpad contacts create)
│   ├── update_contact.py        # Contact update (wrapper → dialpad contacts update)
│   ├── export_sms.py            # Export historical SMS (wrapper → dialpad sms export)
│   ├── create_sms_webhook.py    # Webhook management (wrapper → dialpad webhook create)
│   └── _dialpad_compat.py       # Shared helpers for wrappers
├── generated/                    # OpenAPI-generated CLI
│   ├── dialpad                  # Facade with auth bridge + aliases
│   └── dialpad.openapi          # Full 241-endpoint CLI
├── scripts/
│   └── parity-check.sh          # Verify wrapper/new CLI parity
├── sms_sqlite.py                # SQLite storage with FTS5 (RECOMMENDED)
├── webhook_sqlite.py            # Webhook handler for SQLite
├── send_sms.py                  # Legacy fallback script
├── make_call.py                 # Legacy fallback script
├── lookup_contact.py            # Legacy fallback script
├── export_sms.py                # Legacy fallback script
├── create_sms_webhook.py        # Legacy fallback script
├── sms_storage.py               # Legacy JSON storage (deprecated)
└── webhook_receiver.py          # Legacy webhook handler
```

## Wrapper → Generated CLI Flow

Legacy scripts in `bin/` provide backward compatibility while delegating to the generated CLI:

1. **Wrapper receives** legacy-style arguments
2. **Transforms** to generated CLI format (payload JSON)
3. **Executes** `generated/dialpad` with proper auth
4. **Returns** results in legacy format

This allows gradual migration: old scripts keep working, new features accessible via `generated/dialpad` directly.

## Regeneration

```bash
# 1) Fetch latest Dialpad OpenAPI
curl -fsSL https://dash.readme.com/api/v1/api-registry/58a089fmkn6y1s3 -o openapi.json

# 2) Generate CLI from pinned openapi2cli commit
uvx --from /tmp/openapi2cli openapi2cli generate /tmp/openapi.normalized.json --name dialpad --output generated/dialpad.openapi
```
