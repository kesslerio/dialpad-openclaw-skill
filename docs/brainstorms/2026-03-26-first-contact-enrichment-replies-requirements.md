---
date: 2026-03-26
topic: first-contact-enrichment-replies
---

# First-Contact Enrichment and Reply Drafting

## Problem Frame
When a new inbound SMS or missed call arrives, the current handoff is often too terse. For short messages, a summary is not the useful part. The operator needs the agent to answer the real questions first: whether this person already exists in Attio, whether there is a business or deal attached, what background is available, and what a good reply should say.

## Requirements
- R1. For first-time inbound contacts, the agent output should include whether the person already exists in Attio, whether a company is attached, and whether there is an existing deal or relationship context.
- R2. If Attio does not provide enough identity or business context, the agent should use web research as a fallback to identify the person or business and add a concise background note.
- R3. For first-time inbound SMS or missed-call follow-up, the agent should include a draft SMS reply that directly answers the inbound message or call intent.
- R4. For short inbound messages, the agent should prioritize identity, business context, and draft reply over a verbose summary.
- R5. If the agent identifies a previously unknown contact, the output should make it obvious that the Dialpad contact should be normalized or updated with the newly discovered identity.
- R6. For known contacts with existing context, the agent should say so plainly and keep the response brief.

## Success Criteria
- A first-time inbound message produces a compact response that tells the operator who the contact is, whether they map to an Attio record or business, and what reply to send back.
- Operators no longer need to do a separate manual lookup just to understand who the new contact is.
- The response is useful on a short message without wasting space on a generic summary.

## Scope Boundaries
- Not a full outbound automation system.
- Not a general summary generator for every inbound message.
- Not a requirement to change every existing contact interaction.
- Not a CRM clean-up project for historic data unless a first-contact lookup discovers a clear update opportunity.

## Key Decisions
- Attio is the primary source of truth for identity and relationship context.
- Web research is a fallback only when Attio does not give enough signal.
- The feature is focused on first-time or otherwise unknown contacts, because that is where the manual lookup cost is highest.
- Short inbound messages should get concise enrichment plus a draft reply, not a long narrative summary.

## Dependencies / Assumptions
- Attio and web search are available to the agent in the OpenClaw workflow.
- The agent can surface a draft reply in the same operator handoff that contains the enrichment.

## Outstanding Questions

### Deferred to Planning
- [Affects R5][Technical] Should the workflow only recommend a Dialpad contact update, or should it also attempt the update automatically when the identity is confirmed?
- [Affects R1, R2][Needs research] What exact precedence should the agent use when Attio and web research disagree about the identity or business match?

## Next Steps
→ /prompts:ce-plan
