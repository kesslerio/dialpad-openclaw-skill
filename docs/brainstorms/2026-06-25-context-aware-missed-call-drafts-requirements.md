---
title: feat: context-aware missed-call drafts + agent callback tool
type: feat
date: 2026-06-25
origin: docs/brainstorms/2026-06-24-agent-draft-into-local-card-requirements.md
---

# feat: Context-Aware Missed-Call Drafts + Agent Callback Tool

## Summary

Fix the generic missed-call draft ("sorry we missed your call, I saw your demo context and will follow up shortly") by making the deterministic fallback context-aware (demo booked vs requested, demo timing, urgency flag) and registering a `submit_draft` tool so the AI agent reliably calls back instead of ignoring a URL instruction in the hook message. Also bumps the merged-flow timeout from 30s to 180s.

## Problem

Three issues surfaced from a missed call from Pragada Yogya (+12034916798):

1. **Agent didn't call back:** The merged flow sent the hook with `deliver=false` + a callback URL in the message text. The agent ran, completed, but never POSTed to the callback. The 30s fallback fired with the deterministic draft. Root cause: agents are unreliable at following arbitrary URL instructions buried in a long message — they need a named tool to call.

2. **Deterministic fallback is generic trash:** The draft said "sorry we missed your call. I saw your ShapeScale demo context and will follow up shortly." This tells the operator nothing useful (is the demo booked? when? is it urgent?) and tells the customer nothing actionable. Root cause: the caller had `identityConfidence: low`, which blocks the segment-aware copy path entirely (`_crm_reply_message` line 3905-3906). Even at high confidence, the draft says "I have your demo conversation here" without mentioning demo timing or booking status.

3. **Timeout too short:** 30s isn't enough for the agent to run tools (Attio, Calendar, QMD) and call back. Fixed: bumped to 180s.

## Outcome

- Missed-call drafts tell the operator AND the customer whether the demo is booked or just requested, when it is, and whether it's urgent
- The card surfaces an urgency flag when the demo is within 2 hours
- The agent reliably calls back via a registered `submit_draft` tool instead of a URL instruction
- The deterministic fallback is good enough that the operator gets a useful message even when the agent doesn't call back

## Users

- **Operator (Martin/Sales):** Sees demo status + timing + urgency in the card, gets a relevant draft instead of a generic template, can decide whether to call back immediately (demo imminent) or approve the SMS draft
- **Customer:** Gets a relevant reply ("you're scheduled for a demo tomorrow at 2pm, is there something you wanted to ask?") instead of a generic "will follow up shortly"

## Success Criteria

- R1. Missed-call draft reflects demo status: booked (with date/time) vs requested-but-not-booked (with booking link)
- R2. The card surfaces an urgency flag when the demo is within 2 hours
- R3. The agent calls back via `submit_draft` tool (not a URL instruction) — reliability measured via merged-flow logs
- R4. The deterministic fallback produces a context-aware draft even at low identity confidence (using Attio deal stage from CRM context, not identity-gated)
- R5. Timeout is 180s (configurable)

## Scope Boundaries

### In scope

- Register a `submit_draft` tool in the OpenClaw agent config that POSTs to the webhook's `/internal/draft-callback`
- Rewrite `_crm_reply_message` for missed calls to include demo status + timing from calendar context
- Add urgency flag to the inbound context brief when demo is within 2 hours
- Low-confidence missed calls still get segment-aware copy (gate on CRM match, not identity confidence)
- Bump `DIALPAD_AGENT_DRAFT_TIMEOUT_SECONDS` default to 180

### Deferred for later

- Telegram message editing (replace fallback draft in-place when agent calls back late)
- Multiple candidate drafts
- Structured callback payloads (confidence + sources)

### Outside this product's identity

- Changing the OpenClaw hook protocol
- Removing the deterministic fallback path

## Decisions Resolved

- **Agent callback (B):** Register a `submit_draft` tool. Agents call named tools; they don't follow URL instructions. The tool accepts `jobId` + `draft` parameters and internally POSTs to `/internal/draft-callback`.
- **Smart fallback (C):** Make the deterministic draft context-aware using CRM deal stage + calendar demo timing. Gate on CRM match, not identity confidence — a low-confidence phone match that nonetheless finds an Attio deal should still produce a relevant draft.
- **Urgency flag:** Surface in the card when demo is within 2 hours. Operator decides whether to call or SMS.
- **Timeout:** 180s (3 minutes).

## Open Questions (Resolved)

1. ~~Timeout~~ — 180s
2. ~~Agent callback approach~~ — register a tool (B)
3. ~~Fallback approach~~ — context-aware deterministic draft (C)
4. ~~Urgency~~ — flag in the card header when demo is within 2 hours

## Reference

- Merged flow feature: PR #118, plan `docs/plans/2026-06-24-001-feat-agent-draft-into-local-card-plan.md`
- Phone intel budget fix: `DIALPAD_PHONE_INTELLIGENCE_CACHE_DB` env var was unset, causing `budget_available()` to always return `False` — fixed in `.env`
- Deterministic draft path: `_crm_reply_message` at `webhook_server.py:3894`, `_crm_reply_opening` at `:3888`
- Calendar context for missed calls: available via `create_proactive_reply_draft` → `build_rich_sms_reply` → `build_contextual_sales_sms_reply` → `lookup_sales_calendar_context`
- Agent callback failure evidence: gateway log "hook agent run completed without announcement" + no `draft-callback` hits in webhook logs