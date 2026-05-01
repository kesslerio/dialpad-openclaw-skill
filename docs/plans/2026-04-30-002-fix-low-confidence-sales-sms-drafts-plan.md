---
title: fix: create generic approval drafts for low-confidence Sales SMS
type: fix
status: completed
date: 2026-04-30
origin: docs/brainstorms/2026-04-30-low-confidence-sales-sms-drafts-requirements.md
---

# fix: Create Generic Approval Drafts for Low-Confidence Sales SMS

## Overview

The Dialpad webhook should create approval-gated generic drafts for eligible inbound SMS to the Sales line even when sender identity is low-confidence, payload-only, or local-history-only. Low identity confidence should constrain draft specificity, not suppress draft creation. Safety blockers such as opt-out, sensitive escalation, wrong-line policy, unsupported sender type, and degraded lookup failures still suppress drafting.

This is a narrow policy fix on top of the existing approval ledger. It should not add auto-send, rich product answering, CRM mutation, or Telegram button changes.

## Problem Frame

A sales-positive inbound SMS from a low-confidence sender asked about ShapeScale consumer versus business versions and trial interest. The webhook posted an inbound context brief with `identityConfidence: low` and `draftMode: deterministic_fallback`, but no approval draft was created because the current eligibility gate only allows generic drafts when the Dialpad lookup status is exactly `not_found`. Payload-provided contact names produce `payload_contact`, so they are shown to the operator but rejected for draft creation.

That is the wrong tradeoff now that outbound SMS is approval-gated. The system should still avoid unverified personalization, but it should give the operator a safe generic draft to approve or reject.

## Requirements Trace

- R1-R2. Eligible Sales-line inbound SMS must create an approval draft; low-confidence identity, payload-only names, and local-history-only evidence do not block drafting.
- R3. Existing blockers remain authoritative: opt-out, sensitive escalation, unsupported sender type, wrong-line policy, and degraded lookup failure.
- R4. Drafts remain unsent until a real human approves the exact text through the approval ledger.
- R5-R7. Low-confidence drafts use generic language; high-confidence fresh context may keep context-aware wording; stale or ambiguous context does not drive personalization.
- R8-R10. Telegram handoffs must clearly distinguish draft creation status, draft basis, and identity confidence.
- AE1-AE4. Cover low-confidence eligible SMS with draft, blocked SMS without draft, high-confidence context-aware draft, and stale/ambiguous no-draft clarity.

## Scope Boundaries

- No autonomous SMS sending.
- No product-specific answer generation for consumer-versus-business, pricing, or trial questions in this slice.
- No Telegram inline-button or approval callback changes.
- No CRM, Attio, or Dialpad contact mutation.
- No expansion beyond Sales-line inbound SMS unless implementation discovers shared helper changes that are necessary to keep missed-call behavior unchanged.

## Context & Research

### Relevant Code and Patterns

- `scripts/webhook_server.py` owns inbound SMS normalization, first-contact context, inbound context, proactive draft eligibility, draft persistence, Telegram handoff text, and OpenClaw hook payloads.
- `build_first_contact_context()` currently sets `needsDraftReply` for first-contact candidates, including payload-only contacts.
- `build_inbound_context()` currently sets `draftMode` to `deterministic_fallback` when first-contact fallback would be the draft basis, even if draft creation later rejects the event.
- `should_send_proactive_reply()` currently allows drafts when `contextDraftAllowed` is true or when the sender is not known and lookup status is exactly `not_found`; it rejects payload-only identities.
- `create_proactive_reply_draft()` already enforces opt-out before draft eligibility and persists drafts through `sms_approval`.
- `build_proactive_reply_message()` already has a generic Sales SMS fallback suitable for low-confidence identities.
- `build_inbound_context_brief()` currently renders "Draft basis: no context-aware draft (deterministic_fallback)", which is ambiguous when no draft was created.
- `tests/test_webhook_server.py`, `tests/test_sender_enrichment.py`, and `tests/test_webhook_hooks.py` already cover payload-contact identity, draft eligibility, inbound context, and Telegram handoff behavior.
- `references/openclaw-integration.md` and `references/api-reference.md` document the current contract that known contacts require high confidence for context-aware drafts and that first-contact sales-line replies create approval drafts.

### Institutional Learnings

- Approval-gated SMS remains the hard safety boundary: inbound automation may create drafts but must not directly send.
- Exact phone or similarly strong evidence is still required before any CRM mutation or personalized relationship claim.
- Explicit opt-out is a hard stop and must not produce override drafts.

### External References

- None used. This is a local webhook policy and contract adjustment with sufficient in-repo precedent.

## Key Technical Decisions

- Separate generic draft eligibility from context-aware draft eligibility. `contextDraftAllowed` remains the high-confidence/fresh-context signal; generic fallback draft eligibility covers low-confidence eligible Sales SMS.
- Keep degraded lookup as a blocker. Low confidence is acceptable for generic drafting, but degraded lookup means the system cannot trust the enrichment path enough to draft automatically.
- Preserve generic copy for this fix. The current safe Sales fallback avoids unverified claims and satisfies the approval-gated draft need without creating a product-answering feature.
- Make Telegram wording reflect actual outcome. The handoff should not imply a deterministic fallback draft basis unless a fallback draft can be created or has been created.
- Prefer additive context fields or wording changes over breaking existing hook payloads.

## Open Questions

### Resolved During Planning

- Should low-confidence Sales SMS get drafts? Yes, when otherwise eligible and approval-gated.
- Should low-confidence drafts be personalized? No. They must stay generic and avoid unverified identity, company, meeting, CRM, or deal claims.
- Should this expand into richer product answers? No. That is out of scope for this fix.

### Deferred to Implementation

- Whether to represent generic draft eligibility as a new boolean in `inboundContext` or derive it only inside draft eligibility. Prefer a small explicit field if it improves Telegram clarity without breaking consumers.
- Exact Telegram label text for "generic approval draft available" versus "no draft because blocked".

## Implementation Units

### U1. Adjust Sales SMS draft eligibility for low-confidence contacts

**Goal:** Allow eligible low-confidence Sales SMS to create generic approval drafts while preserving all existing blockers and context-aware restrictions.

**Requirements:** R1, R2, R3, R4, R5, R6

**Files:**
- Modify: `scripts/webhook_server.py`
- Test: `tests/test_sender_enrichment.py`
- Test: `tests/test_webhook_server.py`

**Approach:**
- Update the proactive draft eligibility helper so non-known first-contact candidates are eligible for generic fallback drafts when lookup is non-degraded, even if the identity state is `payload_contact` instead of `not_found`.
- Keep `contextDraftAllowed` as the only path for context-aware drafting.
- Continue requiring the Sales recipient line and valid sender/recipient numbers.
- Keep opt-out, sensitive filtering, short-code/system filtering, wrong-line filtering, and degraded lookup behavior unchanged.
- Ensure draft creation still flows through `create_proactive_reply_draft()` and `sms_approval`, never through direct send.

**Test scenarios:**
- Payload-only inbound SMS to the Sales line with auto reply enabled creates a pending approval draft, returns `auto_reply_status: draft_created`, and does not call Dialpad send.
- Local-history-only or low-confidence first-contact SMS remains eligible for generic fallback when no blocker applies.
- Degraded lookup still returns `not_eligible` and creates no draft.
- Non-Sales recipient line still creates no draft.
- Known high-confidence fresh context still remains eligible through `contextDraftAllowed`.

**Verification:**
- Existing approval and opt-out tests still pass, proving the broader generic eligibility did not bypass safety gates.

### U2. Clarify inbound context draft status in Telegram and hook payloads

**Goal:** Make operator handoffs unambiguous about whether a draft exists, whether a generic draft is merely available, or why no draft was created.

**Requirements:** R7, R8, R9, R10

**Files:**
- Modify: `scripts/webhook_server.py`
- Test: `tests/test_webhook_hooks.py`
- Test: `tests/test_webhook_server.py`
- Test: `tests/test_sender_enrichment.py`

**Approach:**
- Adjust inbound context construction or brief rendering so low-confidence generic fallback is described as generic approval drafting, not "no context-aware draft" in a way that looks like no draft exists.
- When no draft is created because of a blocker, surface the blocker through existing human-only or status messaging instead of implying fallback eligibility.
- Keep identity confidence, evidence, and recency visible as separate concepts from draft availability.
- Preserve additive payload compatibility for `inboundContext`.

**Test scenarios:**
- Low-confidence eligible Sales SMS Telegram alert includes an inbound context block plus an approval draft block.
- Payload-only contact context does not include `exact_phone_match`, but the draft block is present.
- Stale or ambiguous known context that is not generic-eligible does not show misleading `deterministic_fallback` wording.
- Opt-out or human-only blocked alerts still state that no SMS approval draft was created.

**Verification:**
- Telegram text is understandable without checking service logs.

### U3. Update documentation for the generic fallback policy

**Goal:** Align operator and integration docs with the new policy that low-confidence eligible Sales SMS get generic approval drafts.

**Requirements:** R1-R10

**Files:**
- Modify: `references/openclaw-integration.md`
- Modify: `references/api-reference.md`
- Test: `tests/test_openclaw_integration_docs.py`

**Approach:**
- Document the distinction between context-aware draft permission and generic approval draft eligibility.
- State that low-confidence identity suppresses personalization, not approval-gated generic drafts.
- Preserve the no-auto-send, exact-draft approval, and opt-out hard-stop language.

**Test scenarios:**
- Documentation tests continue to assert no auto-send and current-turn verification language.
- Docs mention that low-confidence generic fallback drafts remain approval-gated.

**Verification:**
- Docs match the webhook behavior exposed in tests.

## System-Wide Impact

- Operators should see approval drafts for more eligible Sales SMS, specifically low-confidence/payload-only inbound leads.
- OpenClaw hook payload consumers may see clearer draft-basis/status fields or wording, but existing fields should remain backward-compatible.
- Approval ledger semantics and Telegram approval buttons remain unchanged.
- Missed-call and voicemail behavior should not regress; shared helper changes must be covered by existing tests.

## Risks & Dependencies

- The main risk is over-personalization. Keep generic fallback copy generic and avoid using payload names for low-confidence drafts if necessary.
- Broader eligibility could accidentally draft for blocked content if the implementation bypasses existing policy order. Preserve opt-out and filtering before eligibility expansion.
- Telegram wording could confuse operators if draft availability and draft creation status remain conflated.
- Tests need to assert "draft created but not sent" explicitly so approval-gated behavior stays clear.

## Verification Plan

- `python -m pytest tests/test_sender_enrichment.py tests/test_webhook_server.py tests/test_webhook_hooks.py tests/test_openclaw_integration_docs.py`
- `python -m pytest`

## Sources & References

- Origin: `docs/brainstorms/2026-04-30-low-confidence-sales-sms-drafts-requirements.md`
- Prior plan: `docs/plans/2026-04-29-001-feat-inbound-contact-context-drafts-plan.md`
- Prior plan: `docs/plans/2026-04-24-001-fix-dialpad-sms-approval-gate-plan.md`
- Code: `scripts/webhook_server.py`
- Tests: `tests/test_sender_enrichment.py`, `tests/test_webhook_server.py`, `tests/test_webhook_hooks.py`, `tests/test_openclaw_integration_docs.py`
- Docs: `references/openclaw-integration.md`, `references/api-reference.md`
