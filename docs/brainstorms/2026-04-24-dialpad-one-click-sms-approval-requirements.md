---
date: 2026-04-24
topic: dialpad-one-click-sms-approval
---

# Dialpad One-Click SMS Approval

## Problem Frame
Dialpad inbound SMS automation can currently cross the line from helpful drafting into unsafe outbound messaging. The immediate failure mode is an agent sending or preparing customer replies without a fresh human approval, including after the customer asked for a real person or wanted outreach to stop.

The desired capability is not autonomous SMS. The desired capability is fast human approval: the agent may enrich context and propose an exact reply, but a real operator must authorize any outbound SMS from the review surface.

## Requirements

**Send Authority**
- R1. Inbound Dialpad SMS must never cause an outbound SMS without an explicit human approval action.
- R2. The default behavior for inbound SMS is notification plus draft generation, not sending.
- R3. The preferred approval UX is a Telegram inline button when callback support exists; if not, the fallback must be a command-based approval such as `/send <draft-id>` with the same safety rules.
- R4. Approval must send the exact draft text shown to the operator. Editing the text creates a new draft and resets approval.
- R5. A pending approval is invalidated by any newer inbound customer message, any manual outbound message in the same thread, or any material CRM/calendar state change used by the draft.

**Risk and Escalation**
- R6. Messages that express anger, confusion, request for a real person, legal/compliance concern, or similar elevated risk must require two-step confirmation before sending.
- R7. The first step for a risky message may select the draft, but the second step must explicitly show the risk reason before the SMS is sent.
- R8. Any real Telegram group member may perform the second confirmation, but the agent or bot must never be able to confirm its own draft.
- R9. The system must log the approving actor, timestamp, draft id, risk reason when present, and resulting SMS delivery id when a send succeeds.
- R10. Explicit opt-out language such as "stop", "do not contact me", "don't bother me", or "remove me" must hard-stop automation: no draft, no send button, no two-step override.

**Operator Experience**
- R11. The review message must clearly distinguish draft text from sent text.
- R12. The review message must show whether the draft is normal approval, risky two-step approval, or blocked opt-out/human-only.
- R13. Blocked opt-out/human-only cases should notify the group that automation is not allowed to send on that thread.
- R14. A stale approval attempt must fail closed with a clear reason instead of sending the old draft.
- R15. A failed Dialpad send attempt must remain visibly unsent and must not be described as sent without a fresh Dialpad success result.

## Success Criteria
- A Dialpad SMS can no longer be sent solely because an agent decided it was appropriate.
- Operators can still approve low-risk replies quickly from Telegram when the draft is current.
- Risky replies require an explicit second confirmation that displays the reason for risk.
- Opt-out messages produce no automated outbound SMS path.
- The audit trail can answer who approved which exact draft, when, and what Dialpad SMS id resulted.
- Failed sends are surfaced as failures, not success claims.

## Scope Boundaries
- This does not re-enable fully autonomous SMS sending.
- This does not require solving all CRM identity or contact ambiguity problems; those remain governed by the contact ambiguity guard.
- This does not require Telegram inline buttons if the current OpenClaw/Telegram surface cannot support callbacks yet. Command approval is an acceptable first implementation with the same policy semantics.
- This does not define the final storage schema, endpoint layout, or implementation details. Those belong in planning.

## Key Decisions
- Notification plus one-click approval over auto-send: fast human approval preserves speed while keeping send authority outside the agent.
- Exact-draft approval only: the audit trail must match what the operator saw and approved.
- Approval valid until superseded: approvals remain usable only while no newer customer, outbound, or context-changing event has changed the basis for the draft.
- Two-step approval for risky messages: risky sends are not forbidden by default, but the second confirmation must surface the risk reason.
- Group-member confirmation allowed: operational speed matters, so any real group member may confirm, with audit logging as the compensating control.
- Opt-out hard stop: explicit opt-out language is a compliance and trust boundary, not a workflow inconvenience.

## Dependencies / Assumptions
- Existing Dialpad/OpenClaw integration guidance already treats `approval_required` as the safe default and separates draft generation from send authority.
- Telegram inline callback support has not been confirmed in the current repo scan. Planning must verify whether the target UX is natively supported or should start with command approval.
- The review surface has enough stable thread context to detect newer inbound messages, manual outbound messages, and context-changing updates before sending.

## Approval Flow
```text
Inbound SMS
  -> classify risk
  -> opt-out? block automation and notify human-only
  -> draft reply if allowed
  -> operator approval
  -> stale? fail closed
  -> risky? require second confirmation with risk reason
  -> send exact draft
  -> log actor, draft, risk reason, timestamp, and SMS id
```

## Outstanding Questions

### Resolve Before Planning
- None.

### Deferred to Planning
- [R3][Technical] Does the current OpenClaw Telegram integration support inline buttons and callback handling, or should the first implementation use `/send <draft-id>`?
- [R5][Technical] What is the smallest reliable event set needed to invalidate approvals across Dialpad, manual sends, CRM changes, and calendar changes?
- [R6][Technical] Where should risk classification live so SMS, missed-call follow-up, and future channels share the same policy?
- [R10][Technical] Which opt-out phrases should be exact hard-stop triggers at launch, and which should be treated as risky but not opt-out?

## Next Steps
-> /ce:plan for structured implementation planning
