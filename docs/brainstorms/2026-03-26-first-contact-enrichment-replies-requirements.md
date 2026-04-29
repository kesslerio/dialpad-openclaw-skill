---
date: 2026-04-29
topic: inbound-contact-enrichment-replies
---

# Inbound Contact Enrichment and Approval Drafting

## Summary

Inbound SMS and missed-call handoffs should identify the sender/caller, show the operator the relevant recent sales context, and provide an approval-gated reply draft only when identity confidence and recency are strong enough.

---

## Problem Frame

When an inbound SMS or missed call arrives, the current handoff can be too generic or misleading. A generic missed-call draft is low value when the caller is already a known prospect, and a contact label is risky if Dialpad resolves the wrong name for a phone number.

For short messages and missed calls, a summary is usually not the useful part. The operator needs the agent to answer the real questions first: who this is, why the system believes that, whether there is recent SMS/call or Attio deal context, and whether a reply draft is safe to propose.

---

## Actors

- A1. Operator: Reviews inbound Dialpad handoffs, approves/rejects drafts, and decides when to handle manually.
- A2. Dialpad webhook skill: Receives inbound SMS and missed-call events and prepares the operator handoff.
- A3. OpenClaw/CRM context tools: Provide Attio identity, deal stage, and recent relationship context when available.

---

## Key Flows

- F1. Unknown or first-contact inbound enrichment
  - **Trigger:** An inbound SMS or missed call arrives from a phone number without strong known-contact context.
  - **Actors:** A1, A2, A3
  - **Steps:** The handoff looks for exact identity matches, checks Attio/business context, optionally uses fallback research when Attio is insufficient, then presents a concise brief and approval-gated draft when appropriate.
  - **Outcome:** The operator can understand who the contact might be and decide whether to approve the proposed reply or handle manually.
  - **Covered by:** R1, R2, R3, R4, R11

- F2. Known or recent-contact inbound enrichment
  - **Trigger:** An inbound SMS or missed call arrives from a phone number with an exact contact match or clear recent conversation continuity.
  - **Actors:** A1, A2, A3
  - **Steps:** The handoff checks Dialpad contact identity, recent SMS/call history, and Attio person/deal stage, then presents the context and only proposes a draft when the context is recent enough.
  - **Outcome:** The operator sees why the system thinks this is the right person and gets a relevant draft only when confidence is high.
  - **Covered by:** R5, R6, R7, R8, R9, R10

---

## Requirements

**Identity and context**
- R1. For first-time inbound contacts, the agent output should include whether the person already exists in Attio, whether a company is attached, and whether there is an existing deal or relationship context.
- R2. If Attio does not provide enough identity or business context for a first-time or unknown contact, the agent should use web research as a fallback to identify the person or business and add a concise background note.
- R3. For known contacts, the handoff should include a concise operator brief with the Dialpad contact, recent SMS/call context, and Attio person/deal stage when available.
- R4. Every identity/context brief should show the evidence behind the conclusion, such as exact phone match, recent thread continuity, Dialpad contact, Attio record, or deal stage.
- R5. When identity evidence is weak, conflicting, stale, or unavailable, the handoff should avoid confident identity claims and avoid context-aware drafts.

**Drafting behavior**
- R6. For first-time inbound SMS or missed-call follow-up, the agent should include a draft SMS reply that directly answers the inbound message or call intent when enough context exists to do so safely.
- R7. For known or recent contacts, the agent should propose an approval-gated draft only when the phone match is exact or recent SMS/call history clearly shows thread continuity.
- R8. Recent sales context should drive context-aware drafting only when the relevant conversation or deal activity is no older than 14 days.
- R9. If the best relevant conversation or deal context is older than 14 days, the handoff should provide context only and should not propose a context-aware reply draft.
- R10. Drafting should use a hybrid model: deterministic fallback for generic safe replies, and context-aware drafting when recent identity and sales context are strong enough.
- R11. All outbound SMS replies remain approval-gated; this feature must not auto-send messages.

**Operator handoff**
- R12. For short inbound messages, the agent should prioritize identity, business context, recent relationship context, and draft reply over a verbose summary.
- R13. If the agent identifies a previously unknown contact, the output should make it obvious that the Dialpad contact may need to be normalized or updated, without performing CRM cleanup automatically.
- R14. The handoff should degrade safely when Attio, Dialpad contact lookup, history lookup, or fallback research is unavailable: state what is known, avoid unsupported claims, and fall back to generic approval-gated drafting or context-only output as appropriate.

---

## Acceptance Examples

- AE1. **Covers R3, R4, R7, R8.** Given a missed call from a phone number that exactly matches a known prospect and has a recent Attio deal update within 14 days, when the webhook posts the operator handoff, it includes the prospect identity, recent deal/context evidence, and an approval-gated draft tailored to that context.
- AE2. **Covers R3, R8, R9.** Given a missed call from a known prospect whose last meaningful SMS/call/deal activity is older than 14 days, when the webhook posts the operator handoff, it includes identity and stale-context notes but does not propose a context-aware draft.
- AE3. **Covers R4, R5, R14.** Given Dialpad returns a contact name that conflicts with exact phone or recent-history evidence, when the webhook posts the handoff, it flags the ambiguity, avoids a confident identity claim, and does not produce a context-aware draft.
- AE4. **Covers R6, R10, R11.** Given an unknown inbound SMS asks a generic sales question and no strong CRM context is available, when the webhook posts the handoff, it may provide a deterministic approval-gated draft but does not send it automatically.

---

## Success Criteria
- A first-time inbound message produces a compact response that tells the operator who the contact is, whether they map to an Attio record or business, and what reply to send back when safe.
- A known or recent prospect inbound produces a compact context brief that explains who the person is, why the system believes that, what recent sales context matters, and whether a draft is safe.
- Operators no longer need to do a separate manual lookup just to understand who the new contact is.
- Operators can distinguish a trustworthy draft from a generic fallback because the handoff shows provenance.
- The response is useful on a short message or missed call without wasting space on a generic summary.

---

## Scope Boundaries
- Not a full outbound automation system.
- Not a general summary generator for every inbound message.
- Not a requirement to change every existing contact interaction outside inbound SMS and missed-call handoffs.
- Not a CRM clean-up project for historic data unless a first-contact lookup discovers a clear update opportunity.
- Not an auto-send system; all SMS replies remain approval-gated.
- Not a fuzzy contact matching or dedupe project.
- Not an email/calendar lookup feature in this version.
- Not a change to the Telegram approval-button mechanics.

---

## Key Decisions
- Attio is the primary source of truth for identity and relationship context.
- Dialpad exact phone contact and recent SMS/call history are first-class context sources for inbound handoffs.
- Web research is a fallback only when Attio does not give enough signal.
- The feature covers both first-time/unknown contacts and known/recent contacts, because generic replies are also harmful when there is relevant sales context.
- Known-contact drafts require high confidence: exact phone match or clear recent thread continuity.
- Recent context means no older than 14 days for this version.
- Older-than-14-day context should inform the operator brief but should not drive a context-aware draft.
- Context-aware drafts use a hybrid approach: deterministic fallback for generic cases, richer drafting only when strong recent context exists.
- Short inbound messages should get concise enrichment plus a draft reply, not a long narrative summary.

---

## Dependencies / Assumptions
- Attio and web search are available to the agent in the OpenClaw workflow.
- Dialpad contact lookup and recent SMS/call history are available or can be made available to the webhook/agent handoff.
- The agent can surface a draft reply in the same operator handoff that contains the enrichment.
- The existing SMS approval gate remains the enforcement point for outbound replies.

---

## Outstanding Questions

### Deferred to Planning
- [Affects R13][Technical] Should the workflow only recommend a Dialpad contact update, or should it also attempt the update automatically when the identity is confirmed?
- [Affects R1, R2][Needs research] What exact precedence should the agent use when Attio and web research disagree about the identity or business match?
- [Affects R3, R7, R14][Technical] What exact recent-history sources are available from Dialpad logs, local webhook storage, and Attio, and which should be queried synchronously versus summarized by OpenClaw?
- [Affects R10][Technical] What guardrails should decide when context-aware drafting uses an LLM versus deterministic fallback?

## Next Steps
→ /prompts:ce-plan
