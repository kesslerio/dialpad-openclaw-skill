---
title: fix: Agent SMS authority audit
type: fix
date: 2026-06-29
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# fix: Agent SMS Authority Audit

## Goal Capsule

Make explicitly operator-approved agent SMS sends a supported Dialpad workflow without giving agents the operator-only draft approval token or leaving stale approval drafts behind as ambiguous audit residue.

The current bug is a split-brain send path: `bin/approve_sms_draft.py` correctly requires `DIALPAD_SMS_APPROVAL_TOKEN`, which is intentionally absent from OpenClaw agent runtimes, but an agent can still send the same approved text through `bin/send_sms.py`. The customer SMS succeeds, then the approval ledger only sees an out-of-band outbound message and marks the draft stale with `manual_outbound`. That is operationally safe for delivery, but weak for audit because it loses the operator approval actor, Dialpad SMS id, and the distinction between "random out-of-band send" and "agent direct send after explicit operator approval".

## Product Contract

### Requirements

- **R1.** Agents may send Dialpad SMS through the stable agent wrapper after explicit operator approval in the current turn.
- **R2.** Agent runtimes must not require or receive `DIALPAD_SMS_APPROVAL_TOKEN`; the token remains operator/webhook-only.
- **R3.** When an agent direct send resolves a known approval draft, the ledger records the agent-supplied operator approval context, approval timestamp, Dialpad SMS id, delivery status, and a source marker that distinguishes this path from trusted Telegram/shell ledger approval.
- **R4.** Draft resolution must fail closed before the API call when the draft is stale, opted out, text-mismatched, sender-mismatched, recipient-mismatched, or risky without explicit risk confirmation.
- **R5.** Existing inbound webhook automation and Telegram button approvals remain approval-gated; no inbound event may become an autonomous SMS send.
- **R6.** Wrapper output and docs must tell operators the truth: agent direct send with audit metadata is supported after explicit approval; shell draft approval still requires an operator-only token.
- **R7.** Existing out-of-band outbound webhook invalidation stays as a safety net for direct Dialpad UI sends and other manual sends that do not carry draft metadata.

### Non-Goals

- Do not expose `DIALPAD_SMS_APPROVAL_TOKEN` to AlphaClaw/OpenClaw agent shells.
- Do not remove `approve_sms_draft.py`, Telegram inline approval buttons, or the existing human approval ledger.
- Do not implement a new OpenClaw gateway plugin or Telegram callback owner in this patch.
- Do not change auto-draft generation, CRM enrichment, calendar drafting, or missed-call drafting behavior except where docs must clarify send authority.
- Do not touch the currently dirty `extensions/dialpad-draft-callback/` package files unless implementation proves unavoidable.

### Safety Invariant

No customer-visible SMS may be sent by inbound automation alone. Agent SMS authority is only for current-turn operator-approved tasks; if a draft id is supplied for audit resolution, the exact stored draft, sender, recipient, opt-out state, risk state, and freshness must be validated before Dialpad is called.

## Planning Contract

### Evidence

- `bin/approve_sms_draft.py` requires `DIALPAD_SMS_APPROVAL_TOKEN` before calling `sms_approval.approve_draft()`.
- `scripts/sms_approval.py` rejects bot/agent actors for draft approval and persists exact draft state, actor metadata, risk state, send id, and delivery status.
- `bin/send_sms.py` is the documented stable agent-facing SMS wrapper and already performs sender resolution, dry-run, suspicious currency checks, API-key checks, and JSON status annotation.
- `scripts/webhook_server.py` marks pending drafts stale with `manual_outbound` on outbound Dialpad webhook events. This is a fallback invalidation path, not a rich approval audit path.
- `README.md`, `SKILL.md`, and `references/openclaw-integration.md` currently emphasize that inbound hooks may create approval drafts and that agents must not approve their own drafts.

### Key Technical Decisions

- **KTD1: Keep two different authorities explicit.** `approve_sms_draft.py` remains the trusted human approval path that can send stored draft text through the ledger. `send_sms.py` becomes explicitly capable of agent direct sends with optional approval-audit metadata, without claiming to be ledger approval.
- **KTD2: Validate before sending when resolving a draft.** If `send_sms.py` is asked to resolve a draft, it must load the draft, verify freshness, opt-out state, sender, recipient, exact text, and risk confirmation before calling Dialpad. Validation failures return a JSON error and do not send.
- **KTD3: Persist direct-agent approval metadata in the existing draft row without overstating trust.** A successful audited direct send updates the draft to `sent`, records the supplied approval context in the existing actor/timestamp/send columns for operational continuity, and adds metadata such as `approval_source=agent_direct_send` plus `approval_actor_trust=agent_asserted`. Reports must be able to distinguish this from trusted Telegram/shell approval where the actor identity came from an authenticated callback or token-bearing operator shell.
- **KTD4: Preserve webhook fallback invalidation.** Outbound webhooks still stale unresolved pending drafts as `manual_outbound`; audited direct sends should reach `sent` first, and later outbound webhook invalidation should ignore terminal sent rows.
- **KTD5: Risk remains two-step.** A risky draft cannot be resolved by direct agent send unless the operator supplies an explicit risk-confirmation flag, so the direct path does not weaken the existing `risk_pending` semantics.

### Open Questions

- None blocking. The implementation can choose exact flag names, but they must be self-describing and test-covered.

## Implementation Units

- [ ] **U1: Add ledger support for audited direct sends**

**Goal:** Provide a deterministic helper that preflights and records a direct agent send against an existing approval draft.

**Requirements:** R2, R3, R4, R7

**Files:**
- Modify: `scripts/sms_approval.py`
- Test: `tests/test_sms_approval.py`

**Approach:**
- Add a helper that validates one draft for direct-agent send resolution before any Dialpad API call.
- Compare normalized recipient number, sender number, and stripped exact text against the stored draft.
- Reject stale, sent, failed, rejected, invalidated, opted-out, actor-blocked, actor-not-allowed, and risky-without-confirmation states.
- Add a companion helper that records the successful Dialpad result into the same draft row with `status=sent`, supplied approval context, send id, delivery status, cleared `send_error`, and metadata indicating the direct agent approval source and trust level.
- Permit recovery from a narrow race where the outbound webhook already staled the same draft as `manual_outbound`, but only when the caller supplies the exact draft id and the stored text/sender/recipient still match.

**Test Scenarios:**
- Happy path: pending draft validates, records a successful direct send as `sent`, and stores supplied approval context plus Dialpad SMS id with `approval_actor_trust=agent_asserted`.
- Failure: text mismatch returns a validation failure and does not mutate the draft.
- Failure: recipient or sender mismatch returns a validation failure and does not mutate the draft.
- Failure: stale draft with non-`manual_outbound` reason cannot be resolved.
- Failure: risky draft without direct risk confirmation cannot be resolved.
- Race: a draft staled by `manual_outbound` can be converted to `sent` only with exact matching draft metadata.

- [ ] **U2: Extend `bin/send_sms.py` with optional approval-audit flags**

**Goal:** Let agents use the supported SMS wrapper for explicitly approved sends while closing the matching approval draft when one exists.

**Requirements:** R1, R2, R3, R4, R6

**Files:**
- Modify: `bin/send_sms.py`
- Test: `tests/test_json_contract.py`

**Approach:**
- Add optional flags for draft resolution and approval actor metadata, for example `--resolve-draft-id`, `--approval-actor-id`, `--approval-actor-username`, `--approval-source`, and `--confirm-risk`.
- If no draft id is supplied, preserve current `send_sms.py` behavior exactly.
- If a draft id is supplied, require an approval actor id and run U1 preflight before `require_api_key()` and before calling Dialpad.
- After a successful Dialpad API response, record the audited direct send through U1 and include an `approval_audit` object in the JSON success data that names the direct-send source and actor trust level.
- If post-send audit recording fails unexpectedly, return a truthful wrapper result that says the SMS API call succeeded but audit recording failed; do not pretend the ledger approved it.

**Test Scenarios:**
- Existing send JSON contract remains unchanged when no audit flags are used.
- Audited send preflights the draft before calling `run_generated_json()`.
- Audited send success includes `approval_audit.status == "sent"` and records the Dialpad message id.
- Missing actor metadata with `--resolve-draft-id` is an `invalid_argument` error and does not call Dialpad.
- Draft validation failure is a JSON error and does not call Dialpad.

- [ ] **U3: Clarify docs and runtime guidance**

**Goal:** Remove the misleading implication that agents should load the approval token and document the two supported send lanes.

**Requirements:** R2, R5, R6, R7

**Files:**
- Modify: `README.md`
- Modify: `SKILL.md`
- Modify: `references/openclaw-integration.md`
- Test: `tests/test_openclaw_integration_docs.py`

**Approach:**
- Document that `DIALPAD_SMS_APPROVAL_TOKEN` is intentionally operator/webhook-only.
- Document that agents can use `bin/send_sms.py` after explicit current-turn operator approval, and should pass audit flags when resolving a shown approval draft.
- Preserve and clarify the inbound automation guardrail: inbound hooks may create drafts but must not send directly.
- Keep shell/Telegram draft approval guidance for the trusted human approval path.

**Test Scenarios:**
- Docs assert that the approval token stays out of agent runtime environments.
- Docs assert that operator-approved agent direct SMS is supported through `bin/send_sms.py`, and that its actor context is agent-asserted unless it came through a trusted Telegram/shell approval surface.
- Docs assert that inbound automation remains draft-only and cannot autonomously send customer SMS.

## Verification Contract

Run these commands from the repo root:

```bash
timeout -k5s 60s python3 -m pytest tests/test_sms_approval.py tests/test_json_contract.py tests/test_openclaw_integration_docs.py
timeout -k5s 60s python3 -m pytest tests/test_sender_enrichment.py -k 'outbound_sms_invalidates_pending_approval_draft'
```

If implementation touches webhook behavior beyond docs, also run:

```bash
timeout -k5s 60s python3 -m pytest tests/test_webhook_server.py tests/test_webhook_hooks.py
```

## Definition of Done

- The agent direct-send path no longer depends on `DIALPAD_SMS_APPROVAL_TOKEN`.
- `send_sms.py` can optionally resolve a draft with explicit operator approval context and records a successful audited direct send in `sms_approval_drafts` without conflating it with trusted Telegram/shell approval.
- Mismatched, stale, opted-out, and risky-unconfirmed draft resolution attempts fail before any Dialpad API call.
- The existing no-audit `send_sms.py` behavior remains backward-compatible.
- Inbound automation and Telegram button approval behavior remain gated.
- Docs explain the operator-token path, the agent-authorized direct path, and the fallback `manual_outbound` invalidation path accurately.
- The Verification Contract passes, or any skipped gate is reported with a concrete reason.
