---
status: active
created: 2026-05-14
title: Fix Dialpad SMS History Visibility for Agents
origin: user request in Telegram missed-call triage debugging
---

# Fix Dialpad SMS History Visibility for Agents

## Problem Frame

The Dialpad agent claimed it could not see whether missed-call prospects had already received SMS replies. That was false for the local operating environment: `scripts/sms_sqlite.py` has a populated message store with inbound and outbound messages. The failure came from relying on call history and the broken Stats SMS export wrapper instead of providing a stable agent-facing SMS thread lookup.

## Scope

In scope:
- Make recent call JSON expose the caller phone number when Dialpad provides it.
- Fix the SMS export wrapper so it matches the generated OpenAPI CLI contract.
- Add a stable `bin/` wrapper for inspecting local SMS threads by phone number.
- Document the correct response-check workflow for agents.

Out of scope:
- Changing webhook ingestion behavior.
- Sending, drafting, or approving any SMS.
- Backfilling missing SMS history from Dialpad exports.
- Adding CRM/Attio triage logic.

## Requirements

- Agents must have a deterministic way to check whether a phone number has outbound SMS in local Dialpad history.
- Call-history JSON must include caller phone details, not just display names.
- SMS export must no longer fail with `Missing option '--export-type'`.
- Existing wrapper JSON contracts must stay stable: success/error envelopes remain under `ok`, `command`, `data`, and `meta`.
- Documentation must make the local SMS thread wrapper the first response-state check before agents claim they cannot see sent messages.

## Existing Patterns

- `bin/list_calls.py` is the stable agent-facing call-history wrapper and delegates to `scripts/list_calls.py`.
- `bin/export_sms.py` uses `_dialpad_compat.run_generated_json` for generated CLI calls.
- `scripts/sms_sqlite.py` is operator tooling for the local SMS store; it already exposes thread, search, and stats behavior.
- `tests/test_json_contract.py` validates wrapper JSON envelopes.
- `tests/test_list_calls.py` validates call summary normalization.

## Implementation Units

### U1: Expose Caller Phone in Call Summaries

Files:
- Modify: `scripts/list_calls.py`
- Test: `tests/test_list_calls.py`
- Test: `tests/test_json_contract.py`

Approach:
- Add a caller phone extraction helper that checks `contact.phone`, `external_number`, and `phone_number`.
- Include `contact_phone` in `to_call_summary`.
- Keep `contact` as the display name for backwards compatibility.

Test scenarios:
- A call with both `contact.name` and `contact.phone` returns the name in `contact` and the phone in `contact_phone`.
- A call with no contact name but an external number still exposes that number as `contact_phone`.
- The JSON contract test expects the new field without removing existing fields.

### U2: Fix SMS Export Wrapper CLI Invocation

Files:
- Modify: `bin/export_sms.py`
- Test: `tests/test_json_contract.py`

Approach:
- Replace the `sms export --data ...` invocation with the generated Stats CLI shape:
  `stats stats.create --export-type records --stat-type texts`.
- Pass date filters as `--days-ago-start` and `--days-ago-end`, preserving the existing public wrapper flags.
- Continue polling with `stats stats.get --id`.

Test scenarios:
- `bin/export_sms.py --json` calls `run_generated_json` first with explicit `--export-type records` and `--stat-type texts`.
- Date filters are converted to days-ago arguments.
- Existing timeout/error JSON behavior remains unchanged.

### U3: Add Agent-Facing Local SMS Thread Wrapper

Files:
- Create: `bin/list_sms_thread.py`
- Modify: `README.md`
- Modify: `SKILL.md`
- Modify: `references/api-reference.md`
- Test: `tests/test_json_contract.py`

Approach:
- Build a small wrapper around `scripts.sms_sqlite` that accepts `--phone`, `--limit`, and `--json`.
- Return a stable JSON envelope with the phone number, message count, outbound count, inbound count, latest outbound timestamp, and bounded messages.
- Provide text output for manual operator use.
- Do not expose a send path; this is read-only state inspection.

Test scenarios:
- JSON success for an existing thread includes outbound counts and message summaries.
- Empty thread returns success with `count: 0`, not an error.
- Invalid `--limit` returns the standard wrapper error envelope.

## Verification

Run:
- `python3 -m pytest tests/test_list_calls.py tests/test_json_contract.py -q`
- `python3 -m pytest -q`
- Manual smoke:
  - `python3 bin/list_sms_thread.py --phone +17144763349 --json`
  - `python3 bin/export_sms.py --start-date 2026-05-13 --end-date 2026-05-13 --json --timeout 30 --poll-interval 5`

## Risks

- Dialpad Stats export may complete slowly or require account permissions even after the wrapper invocation is fixed. The wrapper should report upstream failure honestly.
- Local SQLite history only contains what webhook/storage ingestion has captured. The docs should distinguish local history from authoritative all-time Dialpad export.
