# Plan: Issue 58 SMS send status clarity

Date: 2026-03-26
Issues: #58
Depth: Lightweight

## Problem Frame

Issue #58 reports that `bin/send_sms.py` can return a successful SMS result while still surfacing the Dialpad status as `pending`. That is technically accurate as a raw upstream state, but operationally misleading: it reads like the SMS was not sent yet, even when Dialpad has already accepted the request.

The bounded fix in this repo is to make the wrapper response truthful for operators without changing the generated Dialpad API behavior:

- keep the raw upstream payload available
- present `pending` as an accepted/queued state in wrapper output
- preserve the existing wrapper envelope and sender resolution behavior
- add tests that lock the human-readable and JSON surfaces together

This repo does not own Dialpad delivery semantics, carrier delivery finality, or any polling contract beyond what the generated CLI already returns.

## Scope

In scope:

- normalize the SMS send status shown by `bin/send_sms.py`
- preserve the raw Dialpad status in the wrapper JSON payload so the upstream result is not lost
- update tests for both non-JSON and JSON code paths

Out of scope:

- adding a polling loop for final carrier delivery state
- changing the generated Dialpad CLI contract
- changing group intro, call, or contact wrappers unless the same status bug appears there later
- documenting new product behavior outside the wrapper/tests unless the implementation needs it

## Requirements Trace

### Issue #58

- successful SMS sends should not be presented as failed or unsent just because the upstream state is `pending`
- the wrapper should distinguish between accepted/queued, failed, and final delivery state
- truthful operator output matters more than echoing a generic `pending` label

## Context & Patterns

- `bin/send_sms.py` currently prints `result.get("message_status") or result.get("status", "unknown")` and returns the raw JSON result unchanged
- `tests/test_json_contract.py` already exercises the send wrapper JSON envelope with a mocked `status: pending` result
- `tests/test_send_sms_group_intro.py` already covers `send_sms.py` sender resolution and dry-run behavior, making it the best home for the stdout regression
- Other wrappers in `bin/` already keep the generated CLI contract intact while shaping wrapper-specific output, so the right pattern is to add a small status-normalization layer rather than rewriting the send flow

No relevant `docs/solutions/` learning exists in this repo for this specific status-label issue.

## Key Technical Decisions

1. Normalize `pending` to `accepted/queued` in wrapper-facing output.
   Rationale: the issue is about truthful operator messaging, not carrier-final status. A queue/acceptance label matches the actual state better than a bare `pending`.

2. Preserve the raw upstream status alongside the normalized label.
   Rationale: the wrapper should not destroy diagnostic data from Dialpad. Keep the raw state available in JSON so operators and tooling can inspect the upstream value when needed. Also expose a stable wrapper-facing `status` field so consumers do not need to know whether Dialpad used `status` or `message_status`.

3. Keep the change local to `bin/send_sms.py`.
   Rationale: the bug report is about the SMS wrapper. Do not broaden the fix to unrelated wrappers without evidence that they exhibit the same misleading behavior.

## Files

- `bin/send_sms.py`
- `tests/test_json_contract.py`
- `tests/test_send_sms_group_intro.py`

## Implementation Units

### Unit 1: Normalize SMS send status

Goal:
- make `send_sms.py` report an accepted/queued state when Dialpad returns `pending`, while preserving the raw upstream status for debugging

Files:
- `bin/send_sms.py`

Execution note:
- test-first

Approach:
- add a small helper that extracts the upstream status from either `message_status` or `status`
- translate `pending` into a clearer wrapper-facing label such as `accepted/queued`
- preserve the raw upstream status in the wrapper JSON payload under a separate field
- expose a canonical `status`/`status_raw` pair in the wrapper JSON payload, even if the upstream key was `message_status`
- use the normalized label in the human-readable success output

Patterns to follow:
- the existing `send_sms.py` wrapper structure and `emit_success` usage
- the existing JSON wrapper contract in `_dialpad_compat.py`

Verification:
- a successful send with upstream `pending` prints an accepted/queued status instead of a bare `pending`
- JSON mode preserves the raw status and exposes the normalized label

### Unit 2: Lock the wrapper contract with tests

Goal:
- prove the normalized status appears in both stdout and JSON mode without breaking the wrapper envelope

Files:
- `tests/test_send_sms_group_intro.py`
- `tests/test_json_contract.py`

Execution note:
- test-first

Approach:
- add a stdout regression for `send_sms.py` that asserts `pending` is rendered as accepted/queued
- update the JSON contract assertion to check the normalized status and raw status preservation
- keep the existing success envelope shape intact

Patterns to follow:
- existing send-wrapper tests in `tests/test_send_sms_group_intro.py`
- existing JSON envelope assertions in `tests/test_json_contract.py`

Verification:
- the send wrapper still emits the standard success envelope
- stdout no longer teaches operators that a successful send is unsent
- JSON consumers can still inspect the raw Dialpad state if they need it

## Verification

- `pytest -q tests/test_send_sms_group_intro.py tests/test_json_contract.py`
- manual check of `bin/send_sms.py` stdout wording with a mocked pending result

## Risks / Notes

- The main risk is over-correcting the wrapper and losing the upstream raw status. Preserve raw and normalized values together.
- If the generated CLI starts returning a different accepted/queued shape later, the helper should keep the translation narrow and explicit instead of inventing a broader status taxonomy.
