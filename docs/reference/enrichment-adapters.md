# Enrichment context adapters (S1)

Standalone context-command adapters that feed the CRM-aware, calendar-aware, and
QMD-knowledge draft modes in `scripts/webhook_server.py` for Sales SMS and
Sales missed-call approval drafts. Each is invoked as a subprocess: the webhook
appends the query as a single final CLI arg and reads a JSON object from stdout
(contract: `lookup_sales_crm_context` / `lookup_sales_calendar_context` /
`lookup_shapescale_knowledge`).

| Adapter | File | Query in | JSON out |
|---|---|---|---|
| Attio CRM | `scripts/adapters/attio_context.py` | `"<phone> <name> <company>"` | `{usable, status, basis, summary, deal, stage, company, owner, email}` |
| Calendar | `scripts/adapters/calendar_context.py` | `"<name> <email> <company> <deal> <timestamp>"` | `{usable, status, basis, summary, startsInMinutes}` |
| QMD | existing `qmd` binary (no adapter) | `search "<query>"` | `@@`-delimited snippet |

All adapters fail closed (`{"usable": false, ...}`) and exit 0 on any miss, auth
error, or timeout — the webhook treats a non-zero exit as failure.

Silent missed calls can use Attio and calendar context from the caller/CRM
query, but QMD is not applicable unless the normalized call event includes usable
text or transcript-like content. Generic missed-call approval cards render source
statuses so the operator can distinguish not configured, not found, unsafe,
unavailable, and not applicable outcomes.

The calendar adapter surfaces both upcoming demos and bounded recent demos. A
recent missed call after a demo/no-show can therefore become meeting-aware instead
of falling through to generic copy solely because the meeting is already in the
past.

## Secrets (already present in `~/.config/systemd/user/secrets.conf`)

- `ATTIO_API_KEY` — Attio REST bearer token (the adapter calls Attio directly, not the MCP).
- `CALENDLY_API_KEY` — Calendly personal access token (best-effort calendar fallback).

## Env wiring

> **Sequencing:** `DIALPAD_CRM_CONTEXT_COMMAND` and
> `DIALPAD_CALENDAR_CONTEXT_COMMAND` must only be enabled on builds where SMS and
> missed-call draft generation run after the webhook ACK. `.env` is gitignored,
> so these lines remain deploy configuration, not committed state.

```sh
# scripts/adapters invoked by absolute path (systemd PATH is nix-store only)
DIALPAD_QMD_COMMAND=/home/art/.local/bin/qmd
DIALPAD_CRM_CONTEXT_COMMAND=/run/current-system/sw/bin/python3 /home/art/projects/skills/work/dialpad/scripts/adapters/attio_context.py
DIALPAD_CALENDAR_CONTEXT_COMMAND=/run/current-system/sw/bin/python3 /home/art/projects/skills/work/dialpad/scripts/adapters/calendar_context.py
DIALPAD_GOG_CALENDAR_COMMAND=/home/art/.local/bin/shapescale-gog
DIALPAD_GOG_CALENDAR_ACCOUNT=martin@shapescale.com
DIALPAD_GOG_CALENDAR_IDS=primary,alex@shapescale.com,lilla@shapescale.com
# adapters read ATTIO_API_KEY / CALENDLY_API_KEY and gog config from the service environment
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
