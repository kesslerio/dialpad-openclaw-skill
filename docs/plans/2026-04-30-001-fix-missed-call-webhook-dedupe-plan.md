---
title: Fix Missed-Call Webhook Duplicate Notifications
created: 2026-04-30
status: completed
origin: user-reported duplicate missed-call Telegram alerts on 2026-04-30
---

# Fix Missed-Call Webhook Duplicate Notifications

## Problem Frame

Dialpad emits multiple call records for one human-visible inbound department or office call. The webhook currently treats every inbound zero-duration or missed-like call record as an independent missed call, so a single caller can produce multiple Telegram missed-call alerts and multiple approval drafts.

Confirmed production examples from April 30, 2026:

- Main line call from `+14842120084` produced multiple event timestamps for the same visible call.
- Sales line call from `+15865061521` produced parent call `4932532989665280` and child calls `5003912301060096` / `5330302728740864` with `entry_point_call_id=4932532989665280`.

## Scope

In scope:

- Suppress duplicate missed-call side effects for Dialpad parent/child call records representing the same visible inbound call.
- Preserve the first notification/draft/hook for each distinct missed call.
- Return `200 OK` for duplicates so Dialpad does not retry.
- Add test coverage for parent/child duplicate delivery and fallback behavior.
- Improve missed-call logs enough to diagnose future dedupe decisions.

Out of scope:

- Cleaning old Dialpad raw webhook records or subscriptions.
- Changing SMS webhook behavior.
- Changing approval/rejection Telegram button semantics.
- Sending any live SMS or Telegram smoke messages during implementation.

## Key Decisions

- Deduplicate before enrichment, draft creation, OpenClaw hook forwarding, and Telegram delivery in `scripts/webhook_server.py`.
- Prefer Dialpad's root call identity: `entry_point_call_id` when present, otherwise `call_id` / `id`.
- For payloads with no usable call id, use a conservative fingerprint from normalized caller, normalized line, and event timestamp bucket. This protects against retries or equivalent lifecycle duplicates while minimizing suppression of genuinely separate later calls.
- Keep idempotency local to the webhook process/storage layer. A durable SQLite-backed ledger is preferred over process memory so service restarts do not immediately re-notify stale duplicates.
- Store only normalized idempotency keys and timestamps, not raw payloads.

## Implementation Units

### U1: Add Missed-Call Idempotency Helpers

Files:

- Modify: `scripts/webhook_server.py`
- Test: `tests/test_webhook_hooks.py`

Approach:

- Add a small SQLite table for missed-call idempotency, using the existing approval DB path when `sms_approval` is available or a local fallback path otherwise.
- Add helper functions to build a missed-call dedupe key:
  - `entry_point_call_id` first
  - `call_id` / `id` second
  - normalized caller + line + rounded event timestamp bucket as fallback
- Add a claim function that atomically inserts the key and returns whether this webhook invocation owns the side effects.

Test scenarios:

- Root key uses `entry_point_call_id` over child `call_id`.
- Root key falls back to `call_id` when there is no `entry_point_call_id`.
- Fallback key is stable for equivalent caller/line/timestamp payloads.
- Claiming the same key twice returns first-owned, second-duplicate.

### U2: Gate Missed-Call Side Effects

Files:

- Modify: `scripts/webhook_server.py`
- Test: `tests/test_webhook_server.py`

Approach:

- In `handle_call_webhook`, after resolving caller/line/timestamp and before contact enrichment/draft/hook/Telegram side effects, claim the missed-call idempotency key.
- If the event is duplicate, log the duplicate key/root id and return `200 OK` with `duplicate: true`, `telegram_sent: false`, and no draft id.
- If the event is first-seen, continue through the existing enrichment, approval draft, OpenClaw hook, and Telegram path unchanged.
- Include `call_id`, `entry_point_call_id`, and idempotency key in successful missed-call logs.

Test scenarios:

- Parent then child payload sharing `entry_point_call_id` sends one Telegram alert and creates at most one draft.
- Two child payloads sharing `entry_point_call_id` send one Telegram alert.
- A child-only payload still sends one Telegram alert when no prior root event exists.
- Duplicate response stays `200 OK` and marks `duplicate: true`.
- Distinct calls from the same caller/line outside the fallback bucket are not suppressed.

### U3: Verification and Documentation Touch-Up

Files:

- Modify: `README.md`

Approach:

- Add a concise note that missed-call webhooks are idempotency-gated because Dialpad can emit parent/child call records for one visible missed call.
- Verify focused webhook tests and the full pytest suite.

Test scenarios:

- `python3 -m pytest tests/test_webhook_hooks.py tests/test_webhook_server.py`
- `python3 -m pytest`

## Risks

- Over-aggressive fallback dedupe could suppress two real rapid repeat calls from the same number. Mitigation: prefer Dialpad root ids; only use the time-bucket fallback when no id exists.
- A dedupe write failure must fail open or closed intentionally. Recommended behavior: fail open with a warning so missed-call notifications do not silently disappear if local SQLite is unavailable.
- Existing tests heavily patch webhook dependencies; include at least one handler-level test that verifies side effects are skipped, not just helper behavior.

## Verification

- Focused unit tests for idempotency helpers and missed-call handler behavior.
- Full test suite before PR.
- Code review with `ce-code-review mode:autofix plan:docs/plans/2026-04-30-001-fix-missed-call-webhook-dedupe-plan.md`.
