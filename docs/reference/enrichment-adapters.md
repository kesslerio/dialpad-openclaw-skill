# Enrichment context adapters (S1)

Standalone context-command adapters that feed the CRM-aware, calendar-aware, and
QMD-knowledge draft modes in `scripts/webhook_server.py`. Each is invoked as a
subprocess: the webhook appends the query as a single final CLI arg and reads a
JSON object from stdout (contract: `lookup_sales_crm_context` /
`lookup_sales_calendar_context` / `lookup_shapescale_knowledge`).

| Adapter | File | Query in | JSON out |
|---|---|---|---|
| Attio CRM | `scripts/adapters/attio_context.py` | `"<phone> <name> <company>"` | `{usable, status, basis, summary, deal, stage, company, owner}` |
| Calendar | `scripts/adapters/calendar_context.py` | `"<name> <company> <deal> <timestamp>"` | `{usable, status, basis, summary, startsInMinutes}` |
| QMD | existing `qmd` binary (no adapter) | `search "<query>"` | `@@`-delimited snippet |

All adapters fail closed (`{"usable": false, ...}`) and exit 0 on any miss, auth
error, or timeout — the webhook treats a non-zero exit as failure.

## Secrets (already present in `~/.config/systemd/user/secrets.conf`)

- `ATTIO_API_KEY` — Attio REST bearer token (the adapter calls Attio directly, not the MCP).
- `CALENDLY_API_KEY` — Calendly personal access token (best-effort calendar fallback).

## Env wiring — apply at deploy time (U8), NOT yet

> **Sequencing (plan KTD6):** do **not** enable `DIALPAD_CRM_CONTEXT_COMMAND` or
> `DIALPAD_CALENDAR_CONTEXT_COMMAND` in the live `.env` until the async draft
> refactor (U5) and SMS idempotency (U6) ship. The webhook runs draft generation
> inline before the ACK on a single-threaded server; enabling these adds up to
> 8s/call of inline work and would block the webhook. `.env` is gitignored, so
> these lines are a deploy action, not a committed change.

```sh
# scripts/adapters invoked by absolute path (systemd PATH is nix-store only)
DIALPAD_QMD_COMMAND=/home/art/.local/bin/qmd
DIALPAD_CRM_CONTEXT_COMMAND=/home/linuxbrew/.linuxbrew/bin/python3 /home/art/projects/skills/work/dialpad/scripts/adapters/attio_context.py
DIALPAD_CALENDAR_CONTEXT_COMMAND=/home/linuxbrew/.linuxbrew/bin/python3 /home/art/projects/skills/work/dialpad/scripts/adapters/calendar_context.py
# adapters read ATTIO_API_KEY / CALENDLY_API_KEY from the service environment
```

The `qmd` fix alone (absolute path) is safe to land earlier than CRM/calendar —
it repairs an existing call that currently `FileNotFoundError`s — but still adds
latency to the high-confidence inline path, so prefer applying all three together
after async lands.

## Reuse by S2

`attio_context.find_person_by_phone`, `find_person_by_email`, and
`deal_for_person` are the reusable Attio client for the S2 phone-first identity
resolver. Keep them import-safe and side-effect-free.

## Tests

`tests/test_attio_context.py`, `tests/test_calendar_context.py` — HTTP layer
fully mocked (no live API calls). Run: `python3 -m unittest tests.test_attio_context tests.test_calendar_context`.
A guarded live smoke against real Attio was run during U2 and confirmed a real
sender resolves to company/deal/stage.
