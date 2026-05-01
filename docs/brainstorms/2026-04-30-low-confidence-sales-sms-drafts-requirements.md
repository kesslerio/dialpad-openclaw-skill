---
date: 2026-04-30
topic: low-confidence-sales-sms-drafts
---

# Low-Confidence Sales SMS Approval Drafts

## Summary

Eligible inbound SMS messages to the Sales line should create approval-gated reply drafts even when identity confidence is low. Identity confidence should control how specific the draft is, not whether the operator receives a draft at all.

---

## Problem Frame

A sales-positive inbound SMS from a low-confidence contact asked about ShapeScale consumer versus business versions and trial interest. The operator handoff showed local SMS history and webhook contact evidence, but no approval draft was created because the contact evidence was not strong enough for context-aware drafting.

That behavior is too conservative now that outbound SMS is approval-gated. Low-confidence identity is a valid reason to avoid personalized claims, CRM assertions, or prior-relationship wording. It is not, by itself, a valid reason to suppress a generic draft that a human must approve before anything is sent.

The operator experience is also ambiguous when the handoff says the draft basis is `deterministic_fallback` but no draft appears. If the fallback is eligible, a draft should exist. If no draft is created, the handoff should state the blocking reason.

---

## Actors

- A1. Operator: Reviews Telegram handoffs and approves, rejects, or manually handles exact SMS drafts.
- A2. Sales prospect: Sends inbound SMS to the Sales line.
- A3. Dialpad webhook skill: Classifies inbound SMS, builds the operator handoff, and creates approval-gated SMS drafts.

---

## Key Flows

- F1. Eligible low-confidence Sales SMS creates a generic approval draft
  - **Trigger:** An inbound SMS arrives on the Sales line with low-confidence or payload-only identity evidence.
  - **Actors:** A1, A2, A3
  - **Steps:** The handoff records the available identity/context evidence, avoids unverified claims, creates a generic safe draft, and leaves the operator to approve or reject it.
  - **Outcome:** The operator receives a useful draft without any automatic customer send.
  - **Covered by:** R1, R2, R3, R4, R6

- F2. Blocked Sales SMS stays human-only
  - **Trigger:** An inbound Sales SMS contains opt-out language, sensitive/escalation content, unsupported sender type, wrong-line policy, or a degraded lookup failure.
  - **Actors:** A1, A2, A3
  - **Steps:** The handoff posts the inbound message and context that is safe to show, but suppresses the approval draft and states the blocker.
  - **Outcome:** Automation fails closed while the operator can still handle the customer manually.
  - **Covered by:** R1, R3, R7

---

## Requirements

**Draft eligibility**
- R1. Every eligible inbound SMS to the Sales line should create an approval-gated reply draft unless a safety or policy blocker applies.
- R2. Low-confidence identity, payload-only contact names, and local-history-only evidence must not, by themselves, block an approval draft.
- R3. Opt-out language, sensitive escalation, unsupported sender type, wrong-line policy, and degraded lookup failure remain valid blockers that suppress automated drafting.
- R4. All drafts created by this workflow remain unsent until a real human explicitly approves the exact draft.

**Draft specificity**
- R5. Low-confidence drafts must use generic, safe language and must not assert unverified identity, company, relationship history, meeting history, CRM facts, or deal status.
- R6. High-confidence identity with fresh relevant context may continue to use context-aware wording.
- R7. If recent context is stale or ambiguous, the handoff should provide context for the operator but avoid using it as the basis for a personalized draft.

**Operator clarity**
- R8. The Telegram handoff should make draft status unambiguous: either an approval draft is present, or the handoff states why no draft was created.
- R9. A handoff must not imply that deterministic fallback drafting is the selected basis unless the fallback draft is actually available for approval.
- R10. The handoff should keep showing identity evidence and confidence separately from draft availability, so operators understand whether a draft is generic or context-aware.

---

## Acceptance Examples

- AE1. **Covers R1, R2, R4, R5.** Given a low-confidence inbound SMS to Sales with positive sales intent and no blockers, when the webhook processes the message, then Telegram includes an unsent generic approval draft.
- AE2. **Covers R1, R3, R8.** Given an inbound SMS with explicit opt-out or sensitive escalation language, when the webhook processes the message, then no approval draft is created and the handoff states the blocker.
- AE3. **Covers R4, R6, R10.** Given a high-confidence known contact with fresh relevant sales context, when the webhook processes the message, then it may create a context-aware approval draft and clearly show the supporting evidence.
- AE4. **Covers R7, R8, R9.** Given a stale or ambiguous contact context, when no draft is created, then the handoff explains the reason instead of showing `deterministic_fallback` as though a draft should exist.

---

## Success Criteria

- Sales-positive low-confidence inbound SMS messages produce generic approval drafts instead of requiring manual response drafting.
- No SMS is auto-sent by this workflow.
- Operators can tell why a draft is generic, context-aware, or absent without reading service logs.
- The policy can be implemented without inventing new identity or safety rules during planning.

---

## Scope Boundaries

- Not a rich product-answering system for detailed consumer-versus-business comparisons in this slice.
- Not an auto-send system.
- Not a Telegram approval-button change.
- Not a CRM/contact mutation or dedupe workflow.
- Not a replacement for existing opt-out, sensitive-content, sender-type, or degraded-lookup blockers.

---

## Key Decisions

- Approval gating changes the risk calculus: low identity confidence should limit draft specificity, not suppress all drafts.
- Generic deterministic fallback is the default draft mode for low-confidence eligible Sales SMS.
- Strong identity and fresh context are still required before the system uses personalized or CRM-derived wording.
- The operator handoff must distinguish identity confidence, draft basis, and draft creation status.

---

## Dependencies / Assumptions

- The existing SMS approval workflow is active and rejects bot/self-approval.
- Existing opt-out and sensitive-content guardrails remain authoritative blockers.
- The Sales line is the only line covered by this policy change unless planning explicitly expands scope.
- The implementation can create a safe generic draft without depending on Attio, web research, or high-confidence contact identity.

---

## Outstanding Questions

### Deferred to Planning
- [Affects R5][Content] Should the generic fallback stay as a short receipt-style response, or should it include a slightly more helpful sales handoff for obvious product/pricing questions?
- [Affects R8, R10][Technical] What exact labels should the handoff use for draft status, draft basis, identity confidence, and no-draft blocker reason?
- [Affects R3][Technical] Which degraded lookup failures should suppress all drafting versus allow generic fallback with a visible degradation warning?

## Next Steps
-> /ce:plan for structured implementation planning
