---
date: 2026-05-14
topic: meeting-crm-aware-sales-sms-drafts
---

# Meeting and CRM-Aware Sales SMS Drafts

## Summary

Sales SMS approval drafts should retrieve high-confidence business context before drafting. Attio is the primary source for relationship and deal context, while calendar lookup is used only for obvious meeting logistics such as lateness, joining, rescheduling, or meeting links.

---

## Problem Frame

Current context-aware drafts can prove identity and recent Dialpad continuity, but still produce weak customer-facing text. A known prospect saying "I'm running 5 min late" can receive a draft that only says the system saw a recent ShapeScale conversation. That is safe, but it is not useful.

The missing context is not more product knowledge. The useful signal is the prospect's current sales relationship and, for meeting-timing messages, whether a relevant demo or call is happening now or soon. Without that retrieval step, the system has enough confidence to personalize a greeting but not enough understanding to draft the right reply.

---

## Actors

- A1. Operator: Reviews Telegram handoffs and approves, rejects, or manually writes SMS replies.
- A2. Sales prospect: Sends inbound SMS to the ShapeScale Sales line.
- A3. Dialpad webhook skill: Receives inbound SMS, evaluates safety gates, builds the handoff, and creates approval drafts.
- A4. Attio context source: Supplies person, company, owner, deal, stage, and recent relationship context when identity is high-confidence.
- A5. Calendar context source: Supplies current or near-future meeting context only when the inbound SMS is about meeting logistics.

---

## Key Flows

- F1. CRM-aware Sales SMS draft
  - **Trigger:** A high-confidence Sales-line SMS arrives from a known contact or clearly active thread.
  - **Actors:** A1, A2, A3, A4
  - **Steps:** The workflow confirms identity, retrieves compact Attio context, uses that context to choose or shape a short exact-text SMS draft, and shows the basis in the Telegram handoff.
  - **Outcome:** The operator gets a draft that reflects the active sales relationship instead of a vague context-aware acknowledgment.
  - **Covered by:** R1, R2, R3, R6, R7

- F2. Meeting-logistics draft
  - **Trigger:** A high-confidence inbound Sales SMS is clearly about timing, lateness, joining, rescheduling, or a meeting link.
  - **Actors:** A1, A2, A3, A4, A5
  - **Steps:** The workflow retrieves Attio context and checks for a relevant current or near-future calendar event before drafting a concise meeting-aware reply.
  - **Outcome:** The operator gets an approval draft appropriate to the meeting situation, such as a no-worries acknowledgment for a prospect running late.
  - **Covered by:** R1, R4, R5, R6, R8

- F3. Missing or ambiguous context
  - **Trigger:** Attio or calendar context is unavailable, stale, conflicting, or not relevant enough to support a specific draft.
  - **Actors:** A1, A3, A4, A5
  - **Steps:** The workflow avoids unsupported claims, keeps the handoff explicit about what context was unavailable, and falls back to existing generic, context-only, or human-only behavior.
  - **Outcome:** The system does not invent meeting or CRM facts.
  - **Covered by:** R9, R10, R11

---

## Requirements

**Context retrieval**
- R1. For high-confidence inbound SMS to the Sales line, the system should retrieve compact Attio context before creating a context-aware customer-facing draft.
- R2. Attio context should prioritize the contact, company, deal, owner, stage, and recent relationship signal needed to understand the sales situation.
- R3. Attio should be the primary context source for relationship-aware Sales SMS drafts.
- R4. Calendar lookup should be attempted only when the inbound SMS is clearly about meeting timing, joining, lateness, rescheduling, or meeting links.
- R5. Calendar context should be used only when it identifies a relevant current or near-future ShapeScale meeting with enough confidence to support the draft.

**Draft behavior**
- R6. The system should create customer-facing exact-text approval drafts when retrieved context is strong enough to make the reply materially more useful than the current generic or vague context-aware copy.
- R7. CRM-aware drafts should stay short, SMS-friendly, and limited to facts supported by retrieved context.
- R8. For meeting-logistics messages with matching calendar context, drafts should acknowledge the logistics directly rather than saying only that a recent conversation was seen.
- R9. If retrieved context is unavailable, ambiguous, conflicting, stale, or insufficient, the system must not invent CRM, deal, meeting, owner, or relationship facts.

**Safety and operator handoff**
- R10. Existing opt-out, risky-content, sensitive-content, wrong-line, degraded-lookup, and human-only gates remain authoritative.
- R11. All outbound SMS remains approval-gated through the existing SMS approval flow; this capability must not send SMS autonomously.
- R12. The Telegram handoff should show the draft basis clearly enough for the operator to distinguish CRM-aware, meeting-aware, knowledge-backed, generic fallback, context-only, and human-only outcomes.
- R13. The handoff should stay compact and include only the context needed to judge the proposed SMS.

---

## Acceptance Examples

- AE1. **Covers R1, R2, R3, R6, R7, R11.** Given a high-confidence known Sales contact has recent Attio deal context and sends a short follow-up SMS, when the workflow creates a draft, then the draft reflects the active sales context, remains short, and is not sent until approved.
- AE2. **Covers R1, R4, R5, R8, R11.** Given a high-confidence known Sales contact texts "I'm running 5 min late" and a relevant current or near-future demo is found, when the workflow creates a draft, then the proposed SMS directly acknowledges the lateness and remains approval-gated.
- AE3. **Covers R4, R5, R9.** Given an inbound SMS contains timing language but no matching calendar event is found, when the workflow evaluates drafting, then it does not claim there is a meeting and falls back to the safest existing behavior.
- AE4. **Covers R9, R10.** Given Attio returns conflicting or ambiguous relationship context, when the webhook prepares the handoff, then the customer-facing draft avoids CRM-specific claims and the handoff surfaces the ambiguity.
- AE5. **Covers R12, R13.** Given a CRM-aware or meeting-aware draft is proposed, when the operator sees the Telegram handoff, then the basis is visible without turning the alert into a long CRM summary.

---

## Success Criteria

- Operators see materially more useful approval drafts for high-confidence Sales SMS that depend on relationship or meeting context.
- Messages like "I'm running 5 min late" produce meeting-aware approval drafts when a relevant meeting can be verified.
- The system fails closed when Attio or calendar context cannot support a specific customer-facing claim.
- The handoff makes draft basis and evidence clear enough that the operator can approve or reject quickly.
- Planning can proceed without inventing the product boundary between Attio context, calendar context, existing rich product-question drafts, and generic fallback behavior.

---

## Scope Boundaries

- Not an auto-send system.
- Not a full CRM mutation, contact cleanup, or deal-stage update workflow.
- Not broad calendar scanning for every inbound SMS.
- Not a general assistant that writes arbitrary sales copy from all available memory.
- Not a replacement for the existing ShapeScale knowledge-backed product/question draft slice.
- Not a change to Telegram approval-button mechanics.
- Not a requirement to summarize full Attio or calendar records in Telegram.

---

## Key Decisions

- Attio is the primary source for relationship and deal context.
- Calendar is a targeted secondary source for obvious meeting-logistics messages.
- The output includes customer-facing draft text, not only enriched operator context.
- Retrieved context must improve the draft enough to justify using it; otherwise the existing fallback paths should remain.
- Meeting-aware examples are part of v1 because they are common, high-value, and easy for an operator to judge.
- Customer-facing drafts should stay concise and fact-bound; provenance belongs in the operator handoff.

---

## Dependencies / Assumptions

- A high-confidence identity signal is available before CRM-aware or meeting-aware drafting is attempted.
- Attio context is available to the workflow through an existing or planned ShapeScale CRM tool path.
- Calendar context is available to the workflow through an existing or planned ShapeScale Google Workspace tool path.
- The implementation can distinguish obvious meeting-logistics intent well enough to decide when calendar lookup is warranted.
- The existing SMS approval ledger remains the only send authority for inbound-triggered drafts.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R1, R2, R3][Technical] Which Attio lookup path should provide the compact person/company/deal context for the webhook or downstream agent?
- [Affects R4, R5][Technical] Which calendar account, time window, attendee matching rules, and event fields are sufficient to identify a relevant meeting safely?
- [Affects R6, R7, R8][Technical] Should draft generation stay deterministic for v1, use a bounded LLM step with retrieved context, or combine deterministic templates with narrow context slots?
- [Affects R9, R12][Technical] How should the handoff label missing, ambiguous, or conflicting CRM/calendar context without overloading the Telegram alert?
