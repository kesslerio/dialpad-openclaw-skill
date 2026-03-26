---
title: feat: first-contact enrichment and reply drafting
type: feat
status: active
date: 2026-03-26
origin: docs/brainstorms/2026-03-26-first-contact-enrichment-replies-requirements.md
---

# feat: First-Contact Enrichment and Reply Drafting

## Overview

Issue #61 asks for `niemand-work` to do more than summarize first-time inbound SMS and missed calls. The current Dialpad/OpenClaw flow already forwards events, looks up sender contact names, and provides a prompt contract, but the handoff is still too terse for unknown contacts. This plan tightens the outbound hook context and the local `niemand-work` prompt so first-contact inbound events trigger Attio-first enrichment, web fallback when needed, automatic Dialpad contact sync when identity is clear, and a draft reply instead of a generic summary.

## Problem Frame

Short inbound messages and missed calls are not well served by a summary-only handoff. Operators need immediate identity and account context, and when the person is new they need a recommended reply and a clear contact-sync action. The current repo already emits hook events and has a Dialpad Operations prompt with Attio/web/contact-update instructions, but it does not surface a first-contact-specific contract strongly enough to make that behavior reliable.

## Requirements Trace

- R1. First-time inbound contacts should include Attio identity/company/deal context.
- R2. When Attio is insufficient, the workflow should fall back to web research and include a concise background note.
- R3. First-contact SMS and missed-call handoffs should include a draft SMS reply.
- R4. Short messages should prioritize enrichment and a reply over a long summary.
- R5. When a previously unknown contact is identified, the flow should automatically update/create the Dialpad contact when the match is clear.
- R6. Known contacts should be identified briefly and not over-enriched.
- R7. Setup guidance should include generalized examples so users can understand the pattern even if they do not use Attio.

## Scope Boundaries

- Not implementing a full Attio integration inside this repo.
- Not changing the downstream OpenClaw runner semantics beyond the agent contract.
- Not altering message filtering, Telegram alerts, or webhook storage behavior except where first-contact context needs to be carried alongside the existing payload.
- Not building a general summary system for all messages.
- Not documenting every CRM/search provider combination; the docs should show generalized examples instead of a vendor matrix.

## Context & Research

### Relevant Code and Patterns

- `scripts/webhook_server.py` already resolves sender enrichment via `lookup_contact_enrichment()` and builds the normalized hook event payload.
- `scripts/webhook_server.py` already emits `hook_forwarded`, `hook_status`, and sender enrichment fields in webhook JSON responses, which is the right place to expose first-contact eligibility.
- `tests/test_sender_enrichment.py` already exercises inbound SMS hook forwarding, degraded lookup fallback, and enriched-sender behavior.
- `tests/test_webhook_hooks.py` already locks hook-session-key and payload shape.
- `tests/test_webhook_server.py` already locks missed-call forwarding, call classification, and webhook response behavior.
- `bin/create_contact.py` and `bin/update_contact.py` already provide the existing Dialpad create/update writeback wrappers the agent can invoke once identity is confirmed.
- `references/openclaw-integration.md` already documents the downstream expectation of proactive enrichment, a recommended action, and a draft reply.
- `~/.openclaw/openclaw.json` already contains a Dialpad Operations prompt that uses Attio first, falls back to `web_search`, and updates/creates Dialpad contacts, but it needs a more explicit first-contact output contract.
- No relevant `docs/solutions/` learnings exist in this repo.

### Institutional Learnings

- None available for this topic.

### External References

- None used.

## Key Technical Decisions

- Treat Attio as the primary identity source and web search as fallback only when Attio does not provide a confident match.
- Auto-update/create Dialpad contacts only when identity is clear enough to trust; otherwise surface a human-review recommendation rather than forcing writeback.
- Carry a structured first-contact context object in the OpenClaw hook payload rather than relying only on a longer natural-language message.
- Keep SMS and missed-call flows on one shared context shape so the agent prompt can make the same decision regardless of event type.
- Preserve the current webhook success and graceful-degradation behavior even when enrichment is incomplete.

## Open Questions

### Resolved During Planning

- Should confirmed identities trigger contact writeback? Yes. The workflow should auto-update/create the Dialpad contact when the match is clear.

### Deferred to Implementation

- What exact payload field names should carry the structured first-contact context?
- How much of the draft-reply logic should be expressed as prompt contract versus webhook payload hints?
- Should the agent emit a separate human-review flag when Attio and web search disagree, or just suppress writeback?
- What minimum confidence threshold should count as a clear-enough match for automatic Dialpad contact sync?

## Implementation Units

- [ ] **Unit 1: Add first-contact context to webhook payloads**

**Goal:** Make first-time inbound SMS and missed calls carry explicit enrichment data into the OpenClaw hook request.

**Requirements:** R1, R2, R4, R5, R6

**Dependencies:** Existing `lookup_contact_enrichment()`, `normalize_sms_payload()`, and `normalize_call_hook_payload()` helpers.

**Files:**
- Modify: `scripts/webhook_server.py`
- Test: `tests/test_webhook_hooks.py`
- Test: `tests/test_sender_enrichment.py`
- Test: `tests/test_webhook_server.py`

**Approach:**
- Extend the normalized webhook event with a structured first-contact context object built from the existing Dialpad lookup result and message/call metadata.
- Preserve the current hook envelope while adding the enrichment data in a single additive field so downstream OpenClaw can use it without re-parsing the message text.
- Mark first-contact candidates using the current lookup status/degradation signals rather than inventing a new lookup pass.
- Keep the SMS and missed-call shapes aligned so the downstream prompt can reuse the same decision tree.

**Patterns to follow:**
- `build_openclaw_hook_payload()` and `send_to_openclaw_hooks()`
- `lookup_contact_enrichment()` status/degradation shape
- Existing hook payload assertions in `tests/test_webhook_hooks.py`

**Test scenarios:**
- Known inbound contact includes structured identity/company/title context in the hook payload.
- Unknown or unresolved inbound contact is flagged as a first-contact candidate.
- Missed-call hooks carry the same first-contact context shape as SMS hooks.
- Degraded lookup still forwards the event and preserves the fallback context.

**Verification:**
- The payload sent to OpenClaw contains explicit enrichment data for first-contact handling while keeping the existing hook contract intact.

- [ ] **Unit 2: Tighten the `niemand-work` operator contract**

**Goal:** Make the local OpenClaw agent explicitly perform first-contact enrichment, reply drafting, and Dialpad contact sync when the identity is clear.

**Requirements:** R1, R2, R3, R5, R6

**Dependencies:** Unit 1, and the existing Dialpad contact wrappers.

**Files:**
- Modify: `~/.openclaw/openclaw.json`
- Test: `tests/test_webhook_hooks.py`
- Test: `tests/test_sender_enrichment.py`

**Approach:**
- Refine the Dialpad Operations prompt so first-time inbound events require an operator-assist pass before any summary: Attio first, web fallback if needed, auto-update/create when the identity is confirmed, then a draft reply.
- Add a clear output contract for the agent response so it always reports contact/company/deal context, research source, contact-sync action, and draft reply.
- Keep the existing humanizer and Dialpad execution tools in the loop for customer-facing text and writeback actions.
- Make the prompt explicit that short messages should not waste space on a generic summary when the enrichment and reply are the useful outputs.

**Patterns to follow:**
- The current `Dialpad Operations thread` system prompt in `~/.openclaw/openclaw.json`
- Existing Dialpad create/update wrappers in `bin/create_contact.py` and `bin/update_contact.py`
- Operator-facing output contracts documented in `references/openclaw-integration.md`

**Test scenarios:**
- A new sender produces a first-contact assist block instead of a summary-only response.
- A known contact stays brief and skips unnecessary web lookup.
- A clear identity match results in a Dialpad contact update/create recommendation or action.
- A conflicting Attio vs web result does not trigger blind writeback.

**Verification:**
- The operator prompt and response contract consistently instruct `niemand-work` to enrich first, draft second, and sync contacts only when the match is clear.

- [ ] **Unit 3: Document the first-contact workflow contract**

**Goal:** Make the Dialpad/OpenClaw integration docs match the new first-contact behavior so operators and downstream implementers read the same contract.

**Requirements:** R1, R2, R3, R4, R5, R6, R7

**Dependencies:** Units 1 and 2.

**Files:**
- Modify: `README.md`
- Modify: `references/api-reference.md`
- Modify: `references/openclaw-integration.md`

**Approach:**
- Update the OpenClaw integration docs to describe first-contact enrichment as the default assist path for unknown inbound SMS and missed calls.
- Document the structured enrichment context and the expected output sections from `niemand-work`.
- Add generalized setup examples that show the same first-contact pattern with any CRM plus web fallback, not just Attio.
- Call out that confirmed identities can be written back to Dialpad, while ambiguous matches should stay human-reviewed.
- Keep the repo boundary explicit: this repo emits the enriched handoff and the agent contract, while OpenClaw owns the downstream Attio/web/reply execution.

**Patterns to follow:**
- Existing OpenClaw integration language in `references/openclaw-integration.md`
- The current webhook/API docs in `README.md` and `references/api-reference.md`

**Test scenarios:**
- Docs clearly describe the first-contact assist flow for both SMS and missed calls.
- Docs match the actual payload fields and prompt contract.
- Docs distinguish automatic sync from ambiguous human-review cases.

**Verification:**
- The repository docs explain the same first-contact workflow that the hook payload and agent prompt now enforce.

## System-Wide Impact

- Hook payload structure changes will affect both inbound SMS and missed-call OpenClaw deliveries.
- Agent behavior changes will affect the downstream `niemand-work` workspace and the local OpenClaw operator flow, not just the Dialpad webhook server.
- Contact sync behavior touches writeback paths that already exist in `bin/create_contact.py` and `bin/update_contact.py`, so the plan must avoid inventing new contact APIs.
- Failure handling should remain additive: missing enrichment should degrade the handoff quality, not break webhook acceptance or local storage.
- The same first-contact contract should cover SMS and missed-call flows to avoid drift between event types.

## Risks & Dependencies

- The biggest risk is overconfident automation: automatic contact sync is only safe when identity is clear enough to trust. Ambiguous matches must not write back blindly.
- Another risk is prompt drift: if the OpenClaw prompt and webhook payload diverge, the agent will fall back to summary-only behavior again.
- A third risk is false first-contact detection: repeated senders or existing contacts must not be treated as new just because enrichment degraded once.

## Documentation / Operational Notes

- The OpenClaw prompt change lives outside this repo, so it will need a local config reload/restart after the plan is implemented.
- Keep the human-readable operator output short enough for SMS/call follow-up work, but structured enough to show identity, company, deal context, and draft reply.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-03-26-first-contact-enrichment-replies-requirements.md](/home/art/niemand-code/dialpad-openclaw-skill/docs/brainstorms/2026-03-26-first-contact-enrichment-replies-requirements.md)
- Related code: [scripts/webhook_server.py](/home/art/niemand-code/dialpad-openclaw-skill/scripts/webhook_server.py), [bin/create_contact.py](/home/art/niemand-code/dialpad-openclaw-skill/bin/create_contact.py), [bin/update_contact.py](/home/art/niemand-code/dialpad-openclaw-skill/bin/update_contact.py)
- Related docs: [references/openclaw-integration.md](/home/art/niemand-code/dialpad-openclaw-skill/references/openclaw-integration.md), [references/api-reference.md](/home/art/niemand-code/dialpad-openclaw-skill/references/api-reference.md), [README.md](/home/art/niemand-code/dialpad-openclaw-skill/README.md)
- Related config: `/home/art/.openclaw/openclaw.json`
