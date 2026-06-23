# Enrichment context adapters (S1)

Standalone context-command adapters that feed the CRM-aware, calendar-aware,
prior-comms, and QMD-knowledge draft modes in `scripts/webhook_server.py` for Sales SMS and
Sales missed-call approval drafts. Most adapters are invoked as subprocesses: the webhook
appends the query as a single final CLI arg and reads a JSON object from stdout
(contract: `lookup_sales_crm_context` / `lookup_sales_calendar_context` /
`lookup_shapescale_knowledge`).

| Adapter | File | Query in | JSON out |
|---|---|---|---|
| Attio CRM | `scripts/adapters/attio_context.py` | `"<phone> <name> <company>"` | `{usable, status, basis, summary, deal, stage, company, owner, email}` |
| Calendar | `scripts/adapters/calendar_context.py` | `"<name> <email> <company> <deal> <timestamp>"` | `{usable, status, basis, summary, startsInMinutes}` |
| Phone intelligence | `scripts/adapters/phone_intelligence.py` | `"<phone>"` | `{usable, status, phone, line, risk, possibleIdentity}` |
| Public prospect search | `DIALPAD_PUBLIC_PROSPECT_SEARCH_COMMAND` | compact JSON on stdin | `{usable, status, summary, evidence[]}` |
| Prior comms | built into `scripts/webhook_server.py` | phone + CRM email/company | `{usable, status, basis, summary, smsOutboundCount, smsInboundCount, gmailMessageCount}` |
| QMD | existing `qmd` binary (no adapter) | `search "<query>"` | `@@`-delimited snippet |
| Draft model | `scripts/draft_model.py` + configured command | compact facts JSON on stdin | JSON `{message}` or plain text |

All adapters fail closed (`{"usable": false, ...}`) and exit 0 on any miss, auth
error, or timeout — the webhook treats a non-zero exit as failure.

Silent missed calls can use Attio and calendar context from the caller/CRM
query, plus prior comms from local SMS history and strict Gmail search. QMD is
not applicable unless the normalized call event includes usable text or
transcript-like content. Generic missed-call approval cards render source
statuses so the operator can distinguish not configured, not found, unsafe,
unavailable, and not applicable outcomes.

Prior comms retrieval is deterministic by default: local SMS counts/link
evidence and Gmail message counts/dates only. It does not put raw SMS or email
bodies into customer-facing draft text.

Final draft wording can optionally be delegated to a cheap model command via
`DIALPAD_DRAFT_MODEL_COMMAND`. The webhook sends compact facts JSON on stdin:
event metadata, recipient greeting, CRM/calendar/comms source summaries, the
safe deterministic fallback draft, and constraints. The command should return
`{"message":"..."}` or plain text. Output is accepted only if it passes safety
checks; otherwise the deterministic fallback draft is used. The model is a
wording layer, not a source of truth. Deterministic prior-thread link resends
stay deterministic so the auto-send shadow metric continues to measure the
bounded link-resend path without free-text generation.

The calendar adapter surfaces both upcoming demos and bounded recent demos. A
recent missed call after a demo/no-show can therefore become meeting-aware instead
of falling through to generic copy solely because the meeting is already in the
past.

Phone intelligence runs after the webhook idempotency claim and ACK. It is used
only for unknown or low-confidence inbound Sales SMS and missed calls. IPQS
reverse names are possible caller evidence for the operator, never confirmed
identity; they do not raise `identityConfidence` and cannot create a named
customer-facing greeting. Invalid, inactive, disposable/temporary, abusive, or
high-risk numbers become human-only and no customer-facing approval draft is
created. Medium-risk numbers stay eligible only for conservative generic approval
drafts with an operator warning.

The phone adapter reads `IPQS_API_KEY` or `IPQUALITYSCORE_API_KEY` from the
runtime environment. Store the value in the host secret manager and expose it by
environment variable; do not commit literal keys. The observed secret item name
is `IPQUALITYSCORE IPQS API Key`.

Phone results are cached as sanitized normalized fields only, never raw IPQS
payloads. Configure:

```sh
IPQS_API_KEY=${IPQS_API_KEY}
DIALPAD_PHONE_INTELLIGENCE_CACHE_DB=/private/state/dialpad/phone_intelligence.db
DIALPAD_PHONE_INTELLIGENCE_CACHE_TTL_SECONDS=86400
DIALPAD_PUBLIC_PROSPECT_SEARCH_CACHE_TTL_SECONDS=21600
DIALPAD_CALLER_INTELLIGENCE_BUDGET_WINDOW_SECONDS=3600
DIALPAD_PHONE_INTELLIGENCE_MAX_CALLS_PER_WINDOW=120
DIALPAD_PUBLIC_PROSPECT_SEARCH_MAX_CALLS_PER_WINDOW=30
```

The cache directory is created private (`0700`), SQLite files are kept private
(`0600`), expired rows are purged on read/write, and the policy version includes
provider endpoint, strictness, country hint, enhanced-line-check setting, and
risk-threshold version.

`DIALPAD_PUBLIC_PROSPECT_SEARCH_COMMAND` is optional. The webhook sends compact
JSON on stdin containing normalized phone, local format, reverse name, city,
region, country, and risk level. The command must return compact JSON with a
bounded `summary` and `evidence[]` entries containing `sourceType`,
`domainOrTitle`, `matchedTerms`, and `phoneCorroboration`. Page snippets, raw
search result pages, instruction-like text, and sensitive personal details are
rejected. Public search is skipped for invalid, inactive, disposable, medium-risk,
high-risk, insufficient-input, budget-exceeded, or not-configured cases.

Dialpad contact sync is confidence-gated. Automatic create/update may use owned
source identity or public business/professional evidence only when that evidence
directly corroborates the validated phone number. Reverse-name-only,
reverse-name-plus-location, same-name personal results, ambiguous candidates,
conflicts, risky/inactive/invalid callers, timeouts, and budget-degraded cases
produce operator-visible suggestions or warnings instead of writeback. Contact
updates must merge existing identifiers and fill missing fields; they must not
overwrite populated Dialpad fields with lower-confidence enrichment.

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
DIALPAD_GMAIL_CONTEXT_COMMAND=/home/art/.local/bin/shapescale-gog
DIALPAD_GMAIL_CONTEXT_ACCOUNT=martin@shapescale.com
# Optional cheap-model wording layer; default off.
# DIALPAD_DRAFT_MODEL_COMMAND=/path/to/draft-model-command
# DIALPAD_DRAFT_MODEL_TIMEOUT_SECONDS=4
# DIALPAD_DRAFT_MODEL_MAX_CHARS=320
# Optional public prospect search command; default off.
# DIALPAD_PUBLIC_PROSPECT_SEARCH_COMMAND=/path/to/public-prospect-command
# DIALPAD_PUBLIC_PROSPECT_SEARCH_TIMEOUT_SECONDS=4
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

`tests/test_attio_context.py`, `tests/test_calendar_context.py`,
`tests/test_phone_intelligence.py` — HTTP layer fully mocked (no live API calls).
Run: `python -m pytest tests/test_attio_context.py tests/test_calendar_context.py tests/test_phone_intelligence.py`.
A guarded live smoke against real Attio was run during U2 and confirmed a real
sender resolves to company/deal/stage.
