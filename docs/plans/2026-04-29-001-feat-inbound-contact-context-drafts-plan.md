---
title: feat: inbound contact context briefs and approval drafts
type: feat
status: completed
date: 2026-04-29
origin: docs/brainstorms/2026-03-26-first-contact-enrichment-replies-requirements.md
---

# feat: Inbound Contact Context Briefs and Approval Drafts

## Overview

The Dialpad webhook already emits first-contact hints and creates approval-gated generic drafts for unknown sales-line SMS, missed calls, and voicemails. This plan broadens that behavior so known or recent contacts get an operator context brief, and context-aware drafts are created only when identity confidence and recency are strong enough.

The safest first implementation should stay inside the Dialpad/OpenClaw handoff contract: enrich the structured hook payload and Telegram review text with provenance and recency signals, keep deterministic fallback drafts, and leave richer LLM drafting to OpenClaw when the hook payload says it is safe.

## Problem Frame

The current generic missed-call/SMS draft is low value when the inbound number is a known prospect with recent sales context. It also makes wrong-contact incidents harder to catch because the Telegram alert can show a Dialpad label without explaining the evidence behind it. Operators need the handoff to answer "who is this, why do we think so, what recent sales context matters, and is a draft safe?" before any reply is approved.

## Requirements Trace

- R1-R2. Preserve first-time/unknown Attio and fallback identity context in the hook contract.
- R3-R5. Add concise known-contact briefs with Dialpad contact, recent history, Attio/deal placeholders, and evidence/provenance; avoid confident claims and context-aware drafts when evidence is weak, conflicting, stale, or unavailable.
- R6-R11. Keep approval-gated drafting, allow known/recent drafts only for exact phone match or clear recent thread continuity, enforce the 14-day recency boundary, and never auto-send.
- R12-R14. Keep short operator handoffs focused on identity/context/draft, recommend contact normalization without automatic CRM cleanup, and degrade safely when enrichment dependencies fail.
- AE1-AE4. Cover exact-match recent prospect, stale known prospect, conflicting Dialpad identity, and generic unknown inbound cases.

## Scope Boundaries

- No autonomous SMS sending; the existing SMS approval ledger remains the only send path.
- No CRM writeback, contact dedupe, or fuzzy identity matching.
- No email/calendar lookup in this version.
- No change to Telegram inline button mechanics.
- No full in-repo Attio integration unless an existing tool path is already available during implementation; the hook payload can carry placeholders and instructions for OpenClaw/CRM context.

## Context & Research

### Relevant Code and Patterns

- `scripts/webhook_server.py` centralizes the SMS, missed-call, voicemail, OpenClaw hook, Telegram alert, first-contact, and approval-draft behavior.
- `build_first_contact_context()` currently encodes known/unknown/degraded identity into `firstContact`.
- `should_send_proactive_reply()` currently allows drafts only for unknown first-contact `not_found` cases and blocks known contacts.
- `create_proactive_reply_draft()` already persists exact-text approval drafts and invalidates stale drafts via `sms_approval`.
- `build_openclaw_hook_payload()` forwards `firstContact` and `autoReply` as additive payload fields.
- `build_approval_review_suffix()` appends the current draft review block to Telegram messages.
- `tests/test_sender_enrichment.py`, `tests/test_webhook_server.py`, and `tests/test_webhook_hooks.py` already cover sender enrichment, missed-call forwarding, first-contact draft creation, and hook payload shape.
- `references/openclaw-integration.md`, `README.md`, and `references/api-reference.md` document the existing first-contact and approval-draft contract.

### Institutional Learnings

- Existing requirements and docs emphasize strong identity evidence: exact phone/email or similarly strong primary keys beat fuzzy signals.
- Existing approval-gate work established that inbound automation may create drafts but must not send SMS directly.
- Existing opt-out handling must remain a hard stop.

### External References

- None used. Local patterns are sufficient for this bounded webhook and contract change.

## Key Technical Decisions

- Add a general inbound context object rather than overloading `firstContact` further. Keep `firstContact` for backward compatibility and add a clearer context/brief field for all inbound events.
- Treat exact phone contact resolution as high identity confidence unless lookup is degraded or conflicting evidence is present.
- Treat recent thread continuity as a planning-visible signal, but implement the first pass using available local payload/history fields and OpenClaw contract guidance rather than inventing a new CRM store.
- Keep known/recent context-aware drafting as approval-only and conservative: if recency or confidence is unavailable, produce context-only or the existing deterministic fallback.
- Use the 14-day freshness threshold consistently in helper logic, tests, and docs.
- Prefer additive hook/documentation changes so downstream OpenClaw consumers remain compatible.

## Open Questions

### Resolved During Planning

- Should known contacts ever get a draft? Yes, but only when exact phone match or recent thread continuity makes confidence high and relevant context is no older than 14 days.
- Should stale known contacts get a draft? No. They get context only.
- Should this auto-send? No. All replies remain approval-gated.

### Deferred to Implementation

- Which locally available timestamp should be the first recency source for SMS when Dialpad does not supply `last_contacted` style contact metadata?
- Does contact enrichment currently expose enough provenance fields, or should implementation derive provenance from lookup status and normalized phone only?
- Should the Telegram context brief be shown even when no approval draft exists? Default yes, but the implementation should keep it compact.

## Implementation Units

### U1. Add inbound context and recency classification

**Goal:** Build a shared inbound context helper that produces identity confidence, provenance, recency, and draft eligibility hints for SMS, missed calls, and voicemails.

**Requirements:** R3, R4, R5, R7, R8, R9, R14

**Files:**
- Modify: `scripts/webhook_server.py`
- Test: `tests/test_webhook_hooks.py`
- Test: `tests/test_sender_enrichment.py`
- Test: `tests/test_webhook_server.py`

**Approach:**
- Add a helper that consumes normalized event data, sender enrichment, line display, and optional recent-history metadata.
- Output identity state, confidence level, evidence labels, known contact name, event recency state, context age when known, and whether a context-aware draft is allowed.
- Use 14 days as the freshness boundary.
- Preserve `firstContact` exactly enough for existing consumers while adding the new context object to normalized events and OpenClaw payloads.

**Patterns to follow:**
- `build_first_contact_context()`
- `build_openclaw_hook_payload()`
- Existing hook payload assertions in `tests/test_webhook_hooks.py`

**Test scenarios:**
- Exact known contact produces high-confidence context with evidence and `contextDraftAllowed` true when activity is fresh.
- Known contact with stale activity produces context-only output and `contextDraftAllowed` false.
- Degraded or conflicting identity produces low-confidence context and no context-aware draft.
- Unknown contact preserves first-contact behavior and marks identity lookup/business context needs.

**Verification:**
- Hook payloads include both backward-compatible `firstContact` and the new context object for eligible inbound SMS and missed-call events.

### U2. Apply draft eligibility to known/recent inbound events

**Goal:** Change draft creation so known/recent contacts can get approval-gated drafts only when the context helper says it is safe, while stale or ambiguous contacts receive context only.

**Requirements:** R6, R7, R8, R9, R10, R11, R14

**Files:**
- Modify: `scripts/webhook_server.py`
- Test: `tests/test_sender_enrichment.py`
- Test: `tests/test_webhook_server.py`

**Approach:**
- Split "should create an approval draft" from "should create a first-contact generic draft" so known/recent cases can be considered without weakening the unknown-contact guard.
- Keep deterministic fallback messages for unknown/generic cases.
- For known/recent cases, use a conservative contextual draft template only when context is fresh and confidence is high; otherwise do not create a draft.
- Ensure opt-out and risky-policy handling still blocks or requires two-step approval before any draft can be approved.

**Patterns to follow:**
- `should_send_proactive_reply()`
- `build_proactive_reply_message()`
- `create_proactive_reply_draft()`
- SMS approval tests around stale, opt-out, and risky drafts.

**Test scenarios:**
- Recent exact-match missed call creates a pending approval draft and does not send SMS.
- Stale known-contact missed call sends Telegram/OpenClaw context but creates no draft.
- Ambiguous/degraded known-contact inbound invalidates pending drafts and creates no new draft.
- Unknown first-contact SMS still creates the existing generic approval draft.

**Verification:**
- Existing first-contact tests still pass, and new known/recent tests prove no direct SMS send occurs.

### U3. Show compact operator context in Telegram and OpenClaw docs

**Goal:** Make the operator-facing Telegram alert and integration docs explain why a draft exists or why no draft was created.

**Requirements:** R3, R4, R5, R12, R13, R14

**Files:**
- Modify: `scripts/webhook_server.py`
- Modify: `README.md`
- Modify: `references/openclaw-integration.md`
- Modify: `references/api-reference.md`
- Test: `tests/test_sender_enrichment.py`
- Test: `tests/test_webhook_server.py`
- Test: `tests/test_openclaw_integration_docs.py`

**Approach:**
- Add a compact "context brief" block before the approval suffix when inbound context exists.
- Include identity, confidence/evidence, recency, and draft decision; do not include long summaries.
- Document the new hook field and Telegram behavior as additive.
- Keep wording clear that contact normalization is a recommendation, not an automatic writeback.

**Patterns to follow:**
- `build_approval_review_suffix()`
- Existing Telegram Markdown escaping helpers.
- Current OpenClaw receiver contract docs.

**Test scenarios:**
- Telegram missed-call alert for a known recent prospect includes identity/evidence/context and a draft block.
- Telegram alert for stale context includes the brief but no draft block.
- Documentation mentions `firstContact`, new inbound context, approval drafts, 14-day recency, and no auto-send.

**Verification:**
- Operator alerts are understandable without separate lookup, and docs match the payload/test behavior.

## System-Wide Impact

- OpenClaw hook consumers receive an additive context field; existing `firstContact` and `autoReply` fields remain.
- Telegram alerts may become slightly longer, but only to show provenance and draft rationale.
- Approval ledger semantics should remain unchanged.
- Webhook success/degradation behavior must remain fail-open for notifications and fail-closed for opt-out/send authority.

## Risks & Dependencies

- Overconfident identity is the main risk. The implementation must prefer context-only when evidence is degraded, conflicting, or stale.
- Recency may be incomplete if Dialpad payloads do not include usable relationship timestamps. Missing recency should not become a false "fresh" signal.
- Telegram Markdown escaping must cover all dynamic context fields.
- The OpenClaw side may need prompt/config follow-up to use the new context field for richer LLM drafting; this repo should still provide useful deterministic behavior without that follow-up.

## Verification Plan

- `python -m pytest tests/test_webhook_hooks.py tests/test_sender_enrichment.py tests/test_webhook_server.py`
- `python -m pytest tests/test_openclaw_integration_docs.py`
- `python -m pytest`

## Sources & References

- Origin: `docs/brainstorms/2026-03-26-first-contact-enrichment-replies-requirements.md`
- Existing plan: `docs/plans/2026-03-26-002-feat-first-contact-enrichment-replies-beta-plan.md`
- Code: `scripts/webhook_server.py`
- Tests: `tests/test_sender_enrichment.py`, `tests/test_webhook_server.py`, `tests/test_webhook_hooks.py`, `tests/test_openclaw_integration_docs.py`
- Docs: `README.md`, `references/openclaw-integration.md`, `references/api-reference.md`
