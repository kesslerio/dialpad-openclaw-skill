---
status: active
created: 2026-05-15
origin: user-reported ce-debug summary
scope: fix
---

# Fix SMS DB Path Configuration

## Problem

`bin/list_sms_thread.py` depends on `scripts/sms_sqlite.py:init_db()`, which currently uses a module-level hardcoded SMS database path. That works only when `/home/art/clawd/logs/sms.db` is present and writable. In runtimes where the live SMS history exists at `/home/art/niemand/logs/sms.db` and `/home/art/clawd` cannot be created, the wrapper can fail before reading the actual history.

The wrapper also only catches `WrapperError`, so database open/path failures can escape as raw tracebacks instead of the repo's JSON error envelope.

## Scope

- Make the SMS DB path configurable through an environment variable, with the current hardcoded path retained as the default.
- Keep existing callers compatible: `init_db()` should still work with no arguments, and tests can still monkeypatch `sms_sqlite.DB_PATH`.
- Make `bin/list_sms_thread.py --json` return a structured wrapper error when database initialization or thread loading fails.
- Document the environment variable so runtime operators can set it in AlphaClaw or other OpenClaw deployments.

## Non-Goals

- No SMS schema migration.
- No change to how messages are stored or queried.
- No change to approval-draft DB configuration, which already uses `DIALPAD_SMS_APPROVAL_DB`.
- No attempt to infer every possible host-specific DB path automatically.

## Root Cause

Trigger: an agent runs `bin/list_sms_thread.py --phone ... --json` in a runtime where `/home/art/clawd/logs/sms.db` is not accessible.

Causal chain:

1. `bin/list_sms_thread.py` calls `init_db()`.
2. `scripts/sms_sqlite.py` resolves `DB_PATH` to `/home/art/clawd/logs/sms.db`.
3. `init_db()` tries to create `DB_PATH.parent` and open that file.
4. If `/home/art/clawd` cannot be created or reached, SQLite initialization raises an OS/SQLite exception.
5. The wrapper catches only `WrapperError`, so the failure can surface as a raw traceback rather than a JSON envelope.

## Implementation Units

### Unit 1: Configurable SMS DB Path

Files:

- `scripts/sms_sqlite.py`
- `README.md`
- `SKILL.md`
- `references/api-reference.md`

Plan:

- Add `DIALPAD_SMS_DB` support at module load, defaulting to `/home/art/clawd/logs/sms.db`.
- Preserve `DB_PATH` as the single source used by `init_db()` so existing monkeypatch-based tests keep working.
- Use `Path(...).expanduser()` for operator-friendly values.
- Document `DIALPAD_SMS_DB=/home/art/niemand/logs/sms.db` as the AlphaClaw/live override when needed.

Test scenarios:

- With `DIALPAD_SMS_DB` set before importing `sms_sqlite`, `DB_PATH` resolves to that value.
- Existing monkeypatch patterns still work because `init_db()` uses the module-level `DB_PATH`.

### Unit 2: Structured Wrapper Failure

Files:

- `bin/list_sms_thread.py`
- `tests/test_json_contract.py`

Plan:

- Catch database/path exceptions around `init_db()` and `load_thread_summary()`.
- Convert them to `WrapperError` with a stable error code already allowed by the wrapper contract, `internal_error`.
- Keep non-JSON output concise and avoid tracebacks for expected operational failures.
- Preserve current successful JSON shape.

Test scenarios:

- `bin/list_sms_thread.py --json` returns `ok:false` with `command:"list_sms_thread.list"` when `init_db()` raises a permission or SQLite error.
- No Python traceback appears on stdout or stderr.
- The error is retryable or non-retryable according to existing wrapper conventions.

## Verification

- `python -m pytest tests/test_json_contract.py -q`
- `python -m pytest tests/test_sms_sqlite_cache_cleanup.py -q`
- `python -m pytest -q`
- Manual check with `DIALPAD_SMS_DB=/home/art/niemand/logs/sms.db bin/list_sms_thread.py --phone +18184304723 --json`

## Deployment

- Merge the PR with a squash commit.
- Sync the merged skill into AlphaClaw's canonical `/data/.openclaw/skills/dialpad-openclaw-skill` materialized skill directory.
- Set `DIALPAD_SMS_DB=/home/art/niemand/logs/sms.db` in AlphaClaw's env file.
- Restart/recreate AlphaClaw so the env var is loaded.
- Verify the wrapper works inside the container against Theresa's number.
