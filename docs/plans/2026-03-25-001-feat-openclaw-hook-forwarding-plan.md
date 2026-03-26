# Plan: Issue 54 OpenClaw Hook Forwarding

Date: 2026-03-25
Issues: #54
Depth: Standard

## Problem Frame

Issue #54 asks for inbound SMS and missed-call events to reach OpenClaw hooks by default so the downstream agent can do proactive enrichment. This repo already covers part of that path for inbound SMS in `scripts/webhook_server.py`, but the behavior is still SMS-specific, implicitly gated by token presence alone, and the missed-call webhook only sends Telegram notifications.

The bounded, honest slice in this repo is the Dialpad-side emission work:

- make SMS forwarding explicitly default when OpenClaw hooks are configured
- add missed-call forwarding through the same hook transport
- normalize the outbound hook-building path so SMS and call events use one consistent transport
- add rollout flags, tests, and docs so operators can control behavior safely

This repo cannot honestly validate whether OpenClaw accepts a richer payload than the current SMS flow, whether proactive enrichment actually happens downstream, or how OpenClaw handles sessions, dedupe, and prompt behavior in a live deployment. The plan therefore stops at "Dialpad emits the intended hook request shape and preserves graceful webhook behavior."

## Scope

In scope:

- Preserve the existing inbound SMS hook flow while making its default-on behavior explicit when hook config is present
- Add OpenClaw hook forwarding for inbound missed-call events handled by `/webhook/dialpad-call`
- Introduce a shared internal hook-event normalization and send path used by both SMS and calls
- Add config flags so SMS and call hook forwarding can be disabled independently without changing the shared destination config
- Extend tests for payload shape, gating, response fields, and graceful failure handling
- Update webhook documentation for the new behavior and env vars

Out of scope:

- OpenClaw-side proactive enrichment, contact/company lookup, summarization quality, or response recommendation quality
- Live validation against a real OpenClaw `/hooks/agent` deployment
- Changes to voicemail forwarding behavior
- Reworking Dialpad webhook subscription creation flows beyond documenting the expected endpoints
- Changing non-hook Telegram behavior except where response/status symmetry needs minor cleanup

## Requirements Trace

### Issue #54

- Inbound SMS should enter OpenClaw hooks by default
- Inbound missed calls should enter OpenClaw hooks by default
- The downstream agent should be able to act on the forwarded event, but this repo only owns emitting the hook request
- The rollout should reduce manual enrichment work without making the webhook server brittle

### Issue #54 Triage Note

- Do not ship this as one large blob
- SMS hook forwarding already exists and should be cleaned up rather than rewritten
- The next bounded slice is call-event forwarding through the same general path

## Context & Research

- `scripts/webhook_server.py` already forwards eligible inbound SMS to OpenClaw with `build_openclaw_hook_payload()` and `send_sms_to_openclaw_hooks()`
- `scripts/webhook_server.py` already has a strong missed-call classifier and resolver in `resolve_missed_call_context()`, but `handle_call_webhook()` only emits Telegram notifications
- `tests/test_webhook_hooks.py` already covers SMS hook payload shape, auth helpers, and session-key fallback behavior
- `tests/test_sender_enrichment.py` already exercises the inbound SMS webhook handler with mocked hook sends, including sensitive/shortcode filtering and degraded sender enrichment fallback
- `tests/test_webhook_server.py` already covers missed-call detection and resolution primitives, but not the call webhook handler's outbound hook behavior
- `README.md` and `references/api-reference.md` document OpenClaw hook env vars as an SMS-oriented feature today
- There are no prior `docs/solutions/` learnings in this repo to reuse for webhook forwarding or OpenClaw contract handling

External research decision:

- Skipped for planning. The risky boundary here is the repo-local behavior and the unknown downstream OpenClaw contract, not a missing framework pattern. The safest choice is to preserve the existing top-level `/hooks/agent` request envelope and limit changes to repo-controlled normalization, gating, and docs.

## Key Technical Decisions

1. Introduce a shared internal hook-event normalization layer for SMS and missed calls.
   Rationale: The current SMS-only helpers are already doing three jobs at once: normalization, payload building, and sending. Adding missed calls by copy-paste would duplicate transport logic and drift response handling.

2. Preserve the existing top-level OpenClaw request envelope.
   Rationale: This repo does not know whether `/hooks/agent` accepts arbitrary extra fields. The safest Dialpad-side change is to keep sending the current envelope keys (`message`, `name`, `sessionKey`, `deliver`, and optional routing keys) and make the event distinction live in the normalized message text and session key.

3. Add explicit per-event enable flags, but keep the default behavior "on when configured."
   Rationale: Issue #54 wants default-on forwarding. Safe rollout comes from clear opt-out flags and docs, not from burying the behavior behind a second mandatory enable step once OpenClaw hook configuration already exists.

   Concrete plan:
   - `OPENCLAW_HOOKS_SMS_ENABLED` defaults to enabled
   - `OPENCLAW_HOOKS_CALL_ENABLED` defaults to enabled
   - Both flags only matter when `OPENCLAW_HOOKS_TOKEN` is present, so deployments without hook config remain dormant
   - Operators who want legacy SMS-only behavior after deploy can set `OPENCLAW_HOOKS_CALL_ENABLED=0`

4. Reuse the existing missed-call qualification logic for call hook forwarding.
   Rationale: `handle_call_webhook()` already determines when a call event should count as an inbound missed call. The hook path should follow the same qualification rule as the Telegram path so the two surfaces do not drift.

5. Keep voicemail out of this slice.
   Rationale: Issue #54 and the user request are about inbound SMS and missed calls. Voicemail carries different content, volume, and downstream expectations and would turn this bounded change into a broader event-forwarding refactor.

6. Keep a single destination config and a single hook `name` config for both event types in this slice.
   Rationale: Separate SMS/call destinations or event-specific top-level names add rollout complexity without solving a known repo-side problem. The event type can be expressed safely in the normalized message body and session key.

## Assumptions

- The current OpenClaw SMS hook envelope is the only contract we can trust, so new behavior must stay compatible with it
- A stable call hook session key based on `call_id`, then normalized caller number plus event timestamp, then `unknown`, is sufficient on the Dialpad side even though downstream dedupe semantics remain unknown
- Existing operators who already configure OpenClaw hooks prefer default-on behavior for both SMS and calls, with explicit env flags available when they want to suppress one event class

## Open Questions

No blocking product questions remain for this slice.

Implementation-time unknowns that should not block the work:

- Whether OpenClaw treats the missed-call message format as a proactive trigger automatically
- Whether the downstream system needs a different `name` value than the existing configured hook name
- Whether operators will want separate OpenClaw destinations for SMS and calls; this plan intentionally avoids that extra config split

## System-Wide Impact

- `handle_webhook()` and `handle_call_webhook()` will both emit OpenClaw hook status in their JSON responses, which makes the two webhook paths more symmetrical
- Inbound SMS filtering for sensitive content and short codes must remain intact before any hook request is attempted
- Hook transport failures must continue to degrade gracefully so Dialpad still receives HTTP 200 when local storage and classification succeed
- Telegram remains an independent side effect for SMS and missed calls; hook failures must not suppress Telegram, and Telegram failures must not suppress hooks
- Logging will now need to distinguish hook-disabled, hook-filtered, and hook-request-failed states for both SMS and calls

## Files

- `scripts/webhook_server.py`
- `tests/test_webhook_hooks.py`
- `tests/test_webhook_server.py`
- `tests/test_sender_enrichment.py`
- `README.md`
- `references/api-reference.md`

## Implementation Units

### Unit 1: Shared Hook Transport and Event Normalization

Goal:
- Replace the SMS-specific hook sender/builders with a shared internal path that can emit both SMS and missed-call events without changing the top-level OpenClaw request envelope

Files:
- `scripts/webhook_server.py`
- `tests/test_webhook_hooks.py`

Execution note:
- test-first

Approach:
- Add explicit hook-enable env parsing for SMS and call events
- Introduce internal normalized event helpers for `sms` and `missed_call`
- Add a generic session-key builder that preserves the existing SMS key shape and introduces a call-specific key shape
- Add a shared payload builder/sender that preserves the current outbound envelope and routing rules
- Keep the current `OPENCLAW_HOOKS_*` destination config as the single destination surface for this slice
- Keep `OPENCLAW_HOOKS_NAME` as the shared top-level hook name and express the event type inside the normalized message body

Patterns to follow:
- Existing `normalize_sms_payload()`, `build_hook_session_key()`, `build_openclaw_hook_payload()`, and `send_sms_to_openclaw_hooks()` behavior in `scripts/webhook_server.py`
- Existing payload assertions in `tests/test_webhook_hooks.py`

Verification:
- Shared helpers preserve the current SMS payload shape
- Call-event normalization produces a deterministic message and session key with fallback order coverage
- Disabled/misconfigured hook states resolve to explicit status codes without throwing

### Unit 2: SMS Default Forwarding Cleanup

Goal:
- Make the SMS hook path explicitly default-on when configured while preserving current filtering and enrichment behavior

Files:
- `scripts/webhook_server.py`
- `tests/test_sender_enrichment.py`
- `tests/test_webhook_hooks.py`

Execution note:
- test-first

Approach:
- Replace the implicit token-only SMS forwarding gate with explicit event enable/config checks
- Route eligible inbound SMS through the shared hook sender rather than an SMS-only helper
- Preserve sensitive-message and short-code filtering exactly as it works today
- Preserve sender enrichment fallback behavior so cached contact names still feed hook messages when live Dialpad enrichment degrades

Patterns to follow:
- Existing inbound eligibility logic in `assess_inbound_sms_alert_eligibility()`
- Existing mocked hook-send handler tests in `tests/test_sender_enrichment.py`

Verification:
- Eligible inbound SMS still forwards when hooks are configured
- Disabled SMS hooks return a clear response/status without sending
- Sensitive and short-code messages remain blocked before hook send
- Sender enrichment degradation still falls back to cached contact data in hook output

### Unit 3: Missed Call Hook Forwarding

Goal:
- Forward inbound missed-call events from `/webhook/dialpad-call` through the same OpenClaw hook transport while preserving Telegram behavior

Files:
- `scripts/webhook_server.py`
- `tests/test_webhook_server.py`
- `tests/test_webhook_hooks.py`

Execution note:
- test-first

Approach:
- Reuse the existing inbound missed-call qualification already present in `handle_call_webhook()`
- Normalize missed-call context from `resolve_missed_call_context()` into a hook event with caller, line, and timestamp information
- Send the hook request only for inbound missed calls and only when call hooks are enabled/configured
- Extend the call webhook JSON response with `hook_forwarded` and `hook_status` fields so operator-facing behavior mirrors the SMS path
- Keep Telegram delivery on the same qualification path, but do not make one side effect depend on the other succeeding

Patterns to follow:
- Existing call qualification and context-resolution flow in `handle_call_webhook()`
- Existing SMS webhook response/status structure in `handle_webhook()`

Verification:
- Inbound missed calls send a hook request with the expected message/session key
- Outbound calls and answered calls do not send hooks
- Hook request failures still return HTTP 200 with explicit status in the JSON body
- Telegram notifications still run independently of hook success/failure

## Operational / Rollout Notes

- Default behavior after this change:
  - inbound SMS forwards to OpenClaw when `OPENCLAW_HOOKS_TOKEN` is configured and `OPENCLAW_HOOKS_SMS_ENABLED` is not disabled
  - inbound missed calls forward to OpenClaw when `OPENCLAW_HOOKS_TOKEN` is configured and `OPENCLAW_HOOKS_CALL_ENABLED` is not disabled
- Conservative rollout option:
  - deploy with `OPENCLAW_HOOKS_CALL_ENABLED=0` if operators want to observe SMS-only behavior first
- Failure model:
  - storage/classification success should still return HTTP 200 even when OpenClaw hook delivery fails
  - hook status should be visible in JSON responses and logs for both SMS and call endpoints
- Honest validation boundary:
  - local tests can prove request shape, gating, and graceful degradation only
  - production validation still requires a real OpenClaw receiver or a staging test endpoint

### Unit 4: Documentation and Operator Rollout Notes

Goal:
- Document the default-on forwarding behavior and the new operator controls clearly enough that rollout is intentional

Files:
- `README.md`
- `references/api-reference.md`

Approach:
- Update the webhook/OpenClaw section to describe both SMS and missed-call forwarding
- Document the new per-event enable flags and their default-on behavior when hooks are configured
- Make it explicit that voicemail remains a separate Telegram-only path in this slice

Patterns to follow:
- Current env-var documentation style in `README.md`
- Current webhook reference structure in `references/api-reference.md`

Verification:
- README and reference docs describe the same env vars and defaults
- Docs clearly separate supported behavior in this repo from unverified downstream OpenClaw behavior

## Risks & Dependencies

### External Contract Risk

- Risk: `/hooks/agent` may reject even a minimally adjusted payload or may expect a different `message` format for missed calls
- Mitigation: Keep the top-level request envelope unchanged and cover only the repo-controlled request body with tests

### Rollout Surprise

- Risk: Operators with existing OpenClaw hook config will begin forwarding missed calls after deploy
- Mitigation: Add explicit per-event disable flags, document the defaults clearly, and surface hook-disabled vs hook-sent states in logs and JSON responses

### Behavior Drift Between Telegram and Hooks

- Risk: One path may treat an event as a missed call while the other ignores it
- Mitigation: Drive both from the same missed-call qualification and context-resolution flow

### Session-Key Quality

- Risk: Weak fallback keys for call events could collapse unrelated events downstream
- Mitigation: Prefer `call_id`, then phone/timestamp fallbacks, and cover the fallback order in unit tests

## Test Scenarios

1. Verify the shared hook payload builder preserves current SMS envelope behavior.
2. Verify SMS forwarding is sent for eligible inbound SMS when hooks are configured and SMS forwarding is enabled.
3. Verify SMS forwarding is skipped with an explicit disabled status when SMS hooks are turned off.
4. Verify sensitive and short-code inbound SMS still skip both hook forwarding and Telegram SMS alerts.
5. Verify inbound missed calls send a hook request with the expected normalized message and session key.
6. Verify non-missed or non-inbound call events do not send hooks.
7. Verify hook request failures in both SMS and call paths still return HTTP 200 with explicit `hook_status`.
8. Verify docs describe the new flags and clarify that downstream proactive enrichment is not validated in this repo.

## Verification

- `python3 -m pytest tests/test_webhook_hooks.py tests/test_webhook_server.py tests/test_sender_enrichment.py`
- `python3 -m pytest`
- `git diff --check`
- Manual doc sweep for OpenClaw hook env vars and rollout wording

## Alternative Approaches Considered

### Keep Separate SMS and Call Hook Senders

Rejected because it would duplicate transport, routing, and status handling in the same file just to support a second event type.

### Make Missed-Call Hook Forwarding Explicitly Opt-In

Rejected for the first pass because issue #54 explicitly asks for inbound calls and SMS to enter OpenClaw hooks by default. The safer compromise is default-on behavior with independent disable flags.

### Add a Richer Structured Payload to `/hooks/agent`

Rejected because this repo cannot validate the downstream contract. A richer payload would be guesswork disguised as implementation.

## Sources & References

- GitHub issue `#54`: feature request for default inbound call/SMS forwarding to OpenClaw hooks
- GitHub issue `#54` triage comment: split work into SMS-default forwarding and call-event forwarding rather than one blob
- `scripts/webhook_server.py`
- `tests/test_webhook_hooks.py`
- `tests/test_webhook_server.py`
- `tests/test_sender_enrichment.py`
- `README.md`
- `references/api-reference.md`
