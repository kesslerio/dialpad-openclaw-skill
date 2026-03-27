---
date: 2026-03-26
topic: dialpad-contact-ambiguity-guard
---

# Dialpad Contact Ambiguity Guard

## Problem Frame
First-contact inbound SMS and call enrichment is too willing to collapse weak signals into one identity. In the Reggie case, the agent and CRM tooling treated two distinct people with the same first name and adjacent fitness context as the same lead, then tried to recover after the record was already polluted.

That is the wrong failure mode. If identity is not strong enough, the system should stay ambiguous, draft a reply if needed, and block any contact mutation until the evidence is actually strong.

The main fix point is the agent/group instruction layer plus the Attio/ShapeScale CRM contract. Dialpad is the ingress surface, but it is not the primary place where this decision should be made.

## Requirements
- R1. First-contact enrichment must distinguish `resolved`, `ambiguous`, `not_found`, and `degraded` identity states.
- R2. The system must not auto-merge or auto-update a Dialpad contact unless the identity is backed by strong evidence such as an exact phone match, exact email match, or another equally strong primary key.
- R3. First name, area code, industry, job title, and similar soft signals must never be sufficient on their own to confirm identity.
- R4. When identity is ambiguous, the system may still draft a reply or surface context, but it must not mutate the Dialpad contact record.
- R5. If a thread is already linked to a contact, later conflicting evidence must be treated as a conflict, not silently replaced with a similar contact.
- R6. The runtime should preserve provenance for the contact decision so the user can see why a person was considered resolved, ambiguous, or blocked.
- R7. The live OpenClaw prompt for `niemand-work` must reflect the same rule set as the repo contract, but the prompt is a backstop rather than the primary safety mechanism.
- R8. Add regression coverage for false-match cases, including same-first-name collisions, same-industry collisions, and weak area-code-only similarity.

## Success Criteria
- A Reggie-like collision no longer causes a contact merge or update.
- Ambiguous inbound leads still produce useful drafts, but no record mutation occurs without strong evidence.
- The same rules are visible in both runtime policy and repo docs.
- Regression tests fail if a weak match is later allowed to mutate a contact.

## Scope Boundaries
- This does not require a full fuzzy-matching rewrite or ML ranking system.
- This does not remove enrichment or first-contact automation entirely.
- This does not aim to solve every possible CRM dedupe problem across the company.
- This is not primarily a Dialpad wrapper project. Dialpad may need minor pass-through changes, but the main locus is the agent instructions and CRM tooling behavior.

## Key Decisions
- Fail closed on identity: ambiguity is safer than a wrong merge.
- Separate enrichment from mutation: draft and identify can happen without contact writes.
- Treat prompt guidance as a safety backstop, not the only defense.

## Dependencies / Assumptions
- The agent/group instruction layer and CRM tooling are the primary place where first-contact identity is decided.
- OpenClaw prompt policy and repo docs need to stay aligned so the runtime and the written guidance do not diverge.
- Dialpad mainly supplies the ingress event and any minimal metadata needed to preserve the ambiguity state.

## Outstanding Questions

### Resolve Before Planning
- None.

### Deferred to Planning
- [R2][Technical] Where should the ambiguity gate live so both SMS and missed-call paths reuse it cleanly?
- [R6][Technical] What is the smallest evidence payload needed to preserve provenance without making the hook payload noisy?
- [R8][Technical] Which regression fixtures are the best minimal examples for the false-match class?

## Next Steps
→ /prompts:ce-plan for structured implementation planning, starting from the agent/CRM contract rather than the Dialpad wrapper
