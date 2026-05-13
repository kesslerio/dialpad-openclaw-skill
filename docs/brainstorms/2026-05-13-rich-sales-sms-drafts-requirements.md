---
date: 2026-05-13
topic: rich-sales-sms-drafts
---

# Rich Sales SMS Approval Drafts

## Summary

Inbound Sales SMS should get short, useful, approval-only reply drafts for obvious product, booking, link, and pricing questions by using ShapeScale knowledge plus recent Dialpad thread context. The first slice should improve draft quality without changing the no-auto-send safety boundary.

---

## Problem Frame

The current inbound SMS draft behavior is either generic or absent in cases where the operator needs a practical answer. A reply like "The link doesn't work" can be part of an active sales thread, but the deterministic fallback has no understanding of the prior outbound message, booking link, or ShapeScale knowledge needed to propose a useful response.

Existing context work already separates identity confidence, recent Dialpad continuity, and approval-gated draft creation. The remaining gap is content quality: when the question is straightforward and answerable from known ShapeScale material, the operator should not have to manually research and write the obvious reply.

---

## Actors

- A1. Operator: Reviews Telegram handoffs and approves, rejects, or manually edits SMS replies.
- A2. Sales prospect: Sends inbound SMS to the ShapeScale Sales line.
- A3. Dialpad webhook skill: Receives inbound SMS, builds context, and creates approval-gated draft records.
- A4. Knowledge/context layer: Supplies ShapeScale knowledge, recent Dialpad thread context, and optional relationship context for drafting.

---

## Key Flows

- F1. Rich answer draft for an obvious Sales SMS
  - **Trigger:** A Sales-line inbound SMS asks an obvious product, booking, link, or pricing question.
  - **Actors:** A1, A2, A3, A4
  - **Steps:** The workflow reads the inbound message, uses recent Dialpad thread context to understand references, retrieves relevant ShapeScale knowledge when needed, and proposes a short exact-text SMS draft in Telegram.
  - **Outcome:** The operator receives a useful draft that can be approved or rejected without doing separate research.
  - **Covered by:** R1, R2, R3, R4, R7

- F2. Knowledge is insufficient or unsafe
  - **Trigger:** An inbound SMS cannot be answered confidently from available knowledge/context, or a safety blocker applies.
  - **Actors:** A1, A3, A4
  - **Steps:** The workflow avoids unsupported claims, keeps or invalidates drafts according to existing policy, and leaves the operator with context and a human-only or generic handoff as appropriate.
  - **Outcome:** The system does not hallucinate a product answer or create a misleading approval draft.
  - **Covered by:** R5, R6, R8, R9

---

## Requirements

**Draft content**
- R1. For eligible inbound SMS to the Sales line, the system should produce a richer approval draft when the message asks an obvious product, booking, link, or pricing question.
- R2. Rich drafts should answer directly in short SMS-friendly language and include the relevant link or next step when useful.
- R3. Rich drafts should use ShapeScale knowledge as an authoritative source for product, booking, pricing, and common sales-question answers.
- R4. Recent Dialpad SMS history should be used to resolve short references such as "the link," "that price," "the demo," or similar context-dependent replies.

**Safety and fallback**
- R5. If the system cannot answer confidently from available knowledge and recent context, it must not invent an answer; it should fall back to no rich draft, a generic approval draft, or a human-only handoff according to existing policy.
- R6. Existing opt-out, risky-content, sensitive-content, wrong-line, degraded-lookup, and human-only gates remain authoritative and must run before any rich draft can be approved.
- R7. All rich replies remain exact-text approval drafts; no customer SMS may be sent until a real human approves the draft.
- R8. Customer-facing SMS drafts should not include source citations, internal confidence labels, or implementation details.

**Operator handoff**
- R9. The Telegram handoff should make the draft basis clear enough that the operator can distinguish a knowledge-backed draft from a generic fallback or human-only handoff.
- R10. The handoff should stay compact; it should surface only the context needed to judge and approve the proposed SMS.

---

## Acceptance Examples

- AE1. **Covers R1, R2, R3, R7.** Given an inbound Sales SMS asks for the booking link and no safety blocker applies, when the workflow creates a draft, then Telegram shows an unsent SMS draft with the correct booking next step.
- AE2. **Covers R1, R2, R3, R7.** Given an inbound Sales SMS asks an obvious product or pricing question answerable from ShapeScale knowledge, when the workflow creates a draft, then the draft gives a short direct answer without citations and remains approval-gated.
- AE3. **Covers R4.** Given the inbound says "The link doesn't work" after a recent outbound SMS included a booking link, when the workflow creates a draft, then the draft addresses the link issue rather than using the generic receipt-style response.
- AE4. **Covers R5, R6.** Given the inbound contains opt-out language or asks something the knowledge layer cannot answer confidently, when the workflow processes it, then no rich knowledge-backed draft is proposed.
- AE5. **Covers R8, R9, R10.** Given a rich draft is proposed, when the operator sees the Telegram handoff, then the draft basis is visible to the operator but the exact customer-facing SMS remains short and citation-free.

---

## Success Criteria

- Operators receive useful approval drafts for common Sales SMS questions instead of canned acknowledgments.
- Short context-dependent replies like "the link doesn't work" are understood from recent Dialpad thread context.
- Product, booking, pricing, and common sales facts come from ShapeScale knowledge instead of ad hoc model guesses.
- The system fails closed when knowledge/context is missing, ambiguous, stale, or blocked by policy.
- Planning can proceed without inventing the product boundary between rich drafting, generic fallback, and human-only handling.

---

## Scope Boundaries

- Not an auto-send system.
- Not a full CRM/deal-aware personalization engine for every inbound SMS.
- Not a contact creation, Attio update, Dialpad contact normalization, or dedupe workflow.
- Not broad web research by default.
- Not long-form sales copy or multi-paragraph SMS responses.
- Not a replacement for existing opt-out, risky-content, sensitive-content, degraded-lookup, or human-only gates.
- Not a change to the Telegram approval-button mechanics.

---

## Key Decisions

- Primary value is better exact-text reply drafts, not just richer operator context.
- The first slice targets obvious product, booking, link, and pricing questions because those are high-value and constrained enough to answer safely.
- ShapeScale knowledge is the preferred source for factual product/sales answers.
- Recent Dialpad SMS history is required context for short replies that refer back to an active thread.
- Attio and OpenClaw memory may inform future richer drafts, but they are not required for the first slice to be useful.
- Customer-facing drafts should be direct and citation-free; provenance belongs in the operator handoff.

---

## Dependencies / Assumptions

- ShapeScale knowledge is available through a qmd-backed retrieval path or equivalent knowledge search.
- Recent Dialpad SMS history is available enough to understand active-thread references.
- The existing SMS approval ledger remains the only send authority for inbound-triggered drafts.
- The implementation can distinguish common answerable questions from cases that should remain generic or human-only.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R3, R5][Technical] What confidence threshold should decide whether retrieved ShapeScale knowledge is strong enough to draft from?
- [Affects R3][Technical] Which qmd corpus or query pattern should count as authoritative ShapeScale knowledge for product, pricing, booking, and common sales questions?
- [Affects R4][Technical] How much recent Dialpad thread history should be included so the draft can resolve references without overloading the drafting step?
- [Affects R5, R9][Technical] How should the operator handoff label rich draft basis versus generic fallback versus human-only handling?
- [Affects R1, R5][Technical] Should Attio and OpenClaw memory be ignored in v1 except where already available, or opportunistically used when they are cheap and high-confidence?
