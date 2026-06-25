---
title: feat: context-aware missed-call drafts + agent callback tool
type: feat
date: 2026-06-25
origin: docs/brainstorms/2026-06-25-context-aware-missed-call-drafts-requirements.md
---

# feat: Context-Aware Missed-Call Drafts + Agent Callback Tool

## Summary

Fix the generic missed-call draft by (1) making the deterministic fallback context-aware (demo booked vs requested, demo timing, urgency flag), (2) registering a `submit_draft` TypeScript tool plugin so the AI agent reliably calls back instead of ignoring a URL instruction, and (3) bumping the merged-flow timeout to 180s (already done in `.env`).

## Problem Frame

A missed call from Pragada Yogya produced: "sorry we missed your call. I saw your ShapeScale demo context and will follow up shortly." This tells the operator nothing (is the demo booked? when? is it urgent?) and tells the customer nothing actionable. Two root causes: (1) the agent didn't call back within 30s (instruction-following failure — the callback URL was buried in the hook message text), and (2) the deterministic fallback hit the low-confidence gate (`identityConfidence: low`) and produced generic copy, skipping the segment-aware path entirely.

## Requirements

- R1. Missed-call draft reflects demo status: booked (with date/time) vs requested-but-not-booked (with booking link)
- R2. The card surfaces an urgency flag when the demo is within 2 hours
- R3. The agent calls back via a `submit_draft` tool (not a URL instruction) — reliability measured via merged-flow logs
- R4. The deterministic fallback produces a context-aware draft even at low identity confidence (using Attio deal stage from CRM context, not identity-gated) — but never includes company name at low confidence (PII safety)
- R5. Timeout is 180s (already done)

## Scope Boundaries

### In scope

- TypeScript tool plugin (`submit_draft`) that POSTs to `/internal/draft-callback`
- Rewrite `_crm_reply_message` for missed calls: demo booked vs requested, with timing from calendar context
- Relax the low-confidence gate for missed calls: allow segment-aware copy without company names
- Add urgency flag to `build_inbound_context_brief` when demo is within 2 hours
- Update hook message to instruct the agent to call `submit_draft` tool instead of raw HTTP POST

### Deferred for later

- Telegram message editing
- MCP tool alternative (TypeScript plugin is the proper path)
- Multiple candidate drafts

### Outside scope

- Changing the OpenClaw hook protocol
- Removing the deterministic fallback path

---

## Key Technical Decisions

### KTD1: TypeScript tool plugin for `submit_draft`

The dialpad skill is Python; it cannot define OpenClaw tools. A TypeScript plugin using `defineToolPlugin` from `openclaw/plugin-sdk/tool-plugin` is the proper mechanism. The tool accepts `jobId` + `draft` parameters and internally POSTs to `http://127.0.0.1:{PORT}/internal/draft-callback` with the `X-Callback-Token` header. The plugin is installed into the AlphaClaw extensions directory.

*(see origin: requirements doc — Decisions Resolved, B)*

### KTD2: Relax low-confidence gate for missed calls, preserve PII safety

The current gate (`confidence != "high"` → generic copy) exists for PII safety: naming a company at low confidence risks addressing the wrong person (reused/ported phone). The relaxation: allow segment-aware copy (prospect_demo, prospect_cold) at any confidence **when company is empty** — segment framing without company names is PII-safe. The `opening` ("sorry we missed your call") is safe at any confidence. Company names remain gated at `confidence == "high"`.

*(see origin: requirements doc — Decisions Resolved, C)*

### KTD3: Urgency flag from calendar `startsInMinutes`

`lookup_sales_calendar_context` returns `startsInMinutes` (int or None) and `demoState` ("upcoming", "recent", "not_found"). When `demoState == "upcoming"` and `startsInMinutes <= 120`, stamp `inbound_context["urgency"] = "demo in {N} min — consider calling back"` on the event. Render it in `build_inbound_context_brief` after the Segment line.

### KTD4: Demo-status-aware missed-call draft text

Three draft paths for missed calls when `segment == "prospect_demo"`:
- **Demo booked** (`demoState == "upcoming"`): "Hi {name}, sorry we missed your call. You're scheduled for a demo {timing}. Is there something you wanted to ask ahead of time? Happy to call back if easier."
- **Demo requested, not booked** (`stage == "demo request"` + `demoState == "not_found"`): existing copy (line 3926-3931) — "I saw your demo request and that booking may not have gone through. You can grab a time here: {link}..."
- **Demo completed/recent** (`demoState == "recent"`): "Hi {name}, sorry we missed your call. I saw your recent demo — happy to follow up on any questions that came up."

---

## Implementation Units

### U1: `submit_draft` TypeScript tool plugin

**Files:** `extensions/dialpad-draft-callback/openclaw.plugin.json`, `extensions/dialpad-draft-callback/src/index.ts`, `extensions/dialpad-draft-callback/package.json`

Create a minimal TypeScript plugin using `defineToolPlugin`:
- Tool name: `submit_draft`
- Parameters: `jobId` (string), `draft` (string)
- Execute: HTTP POST to `http://127.0.0.1:8081/internal/draft-callback` with `X-Callback-Token` header (from a config field or env var) and `{"jobId": "...", "draft": "..."}` body
- Returns `{status: "delivered", jobId}` on 200, `{status: "lost", jobId}` on 200-with-lost, error on non-200

Install into AlphaClaw extensions directory. The `niemand-work` agent has no `tools` block (default resolution), so the tool is available automatically.

**Tests:** Manual verification — trigger a merged-flow event and confirm the agent calls `submit_draft` instead of ignoring the URL instruction.

### U2: Rewrite `_crm_reply_message` for missed calls

**Files:** `scripts/webhook_server.py`

Modify `_crm_reply_message` (line 3894):

1. **Relax the low-confidence gate for missed calls:** When `is_missed_call` and `confidence != "high"`, instead of returning the generic line immediately, check if `crm_context` has a usable `stage`. If so, proceed to segment classification but force `with_company = ""` (no company name at low confidence). If no CRM context, fall through to the existing generic line.

2. **Add demo-status-aware copy for `prospect_demo` missed calls:**
   - When `demoState == "upcoming"` and `startsInMinutes` is available: include timing in the draft ("You're scheduled for a demo in {N} minutes" or "tomorrow at {time}")
   - When `demoState == "recent"`: "I saw your recent demo — happy to follow up"
   - When `stage == "demo request"` + `demoState == "not_found"`: existing copy (booking link)
   - Fallback: existing "I have your demo conversation here" copy

3. **Stamp urgency on `inbound_context`:** When `demoState == "upcoming"` and `startsInMinutes <= 120`, set `inbound_context["urgency"] = f"demo in {startsInMinutes} min — consider calling back"`.

**Tests:** `tests/test_sender_enrichment.py`

- `test_missed_call_low_confidence_with_crm_gets_segment_copy_without_company` — low confidence + CRM match → segment-aware copy, no company name
- `test_missed_call_low_confidence_no_crm_gets_generic` — low confidence + no CRM → generic copy (existing behavior)
- `test_missed_call_demo_booked_includes_timing` — high confidence + demo booked + upcoming → draft includes "scheduled for a demo"
- `test_missed_call_demo_requested_not_booked_includes_link` — high confidence + demo request + not_found → draft includes booking link (existing behavior preserved)
- `test_missed_call_demo_recent_mentions_followup` — high confidence + demo recent → draft mentions recent demo
- `test_missed_call_urgency_stamped_when_demo_within_2h` — demo in 90 min → `inbound_context["urgency"]` is set

### U3: Urgency flag in `build_inbound_context_brief`

**Files:** `scripts/webhook_server.py`

Modify `build_inbound_context_brief` (line 4462):

After the Segment line (around line 4528), add:
```
urgency = inbound_context.get("urgency")
if urgency:
    lines.append(f"⚠️ *Urgency:* {escape_telegram_markdown(urgency)}")
```

**Tests:** `tests/test_sender_enrichment.py`

- `test_inbound_context_brief_includes_urgency_when_set` — `inbound_context["urgency"]` is set → brief includes urgency line
- `test_inbound_context_brief_no_urgency_when_not_set` — no urgency → brief does not include urgency line

### U4: Update hook message to use `submit_draft` tool

**Files:** `scripts/webhook_server.py`

Modify `format_hook_message` (line 4907):

Replace the raw HTTP POST callback instruction with a tool-call instruction:
```
When you have a draft reply ready, call the submit_draft tool with:
- jobId: "{callback_job_id}"
- draft: "<your draft reply text>"
```

Keep `callback_url` and `callback_token` in the payload for backward compat (the tool plugin handles the POST internally, but the webhook still needs to know the callback params for the fallback timer).

**Tests:** `tests/test_sender_enrichment.py`

- `test_hook_message_uses_submit_draft_tool_when_merged_flow_active` — callback instruction mentions `submit_draft` tool, not raw HTTP POST
- `test_hook_message_unchanged_when_no_callback` — backward compat

### U5: Install plugin + bump timeout default

**Files:** `extensions/dialpad-draft-callback/` (install into AlphaClaw), `scripts/webhook_server.py` (default timeout)

- Install the `submit_draft` plugin into `~/.local/state/alphaclaw/.openclaw/extensions/`
- Restart AlphaClaw to pick up the new plugin
- Change `DIALPAD_AGENT_DRAFT_TIMEOUT_SECONDS` default from 30 to 180 in `webhook_server.py`
- The `.env` already has `DIALPAD_AGENT_DRAFT_TIMEOUT_SECONDS=180` (set earlier today)

**Tests:** Manual — verify the tool appears in the agent's tool list after restart.

---

## System-Wide Impact

- **Operators:** See demo status + timing + urgency in missed-call cards. Get relevant drafts instead of generic templates. Can decide whether to call back immediately (demo imminent) or approve the SMS.
- **Agent (niemand-work):** Gets a `submit_draft` tool — more reliable than URL instructions. No agent config change needed (default tool resolution).
- **OpenClaw gateway:** New TypeScript extension installed. Requires AlphaClaw restart.
- **Dialpad webhook:** Timeout default bumped to 180s. Hook message changes to reference the tool.

## Dependencies

- `defineToolPlugin` from `openclaw/plugin-sdk/tool-plugin` — available in OpenClaw 0.9.18
- AlphaClaw restart to load the new plugin
- `DIALPAD_PHONE_INTELLIGENCE_CACHE_DB` — already fixed (was returning `budget_exceeded` for every lookup)

## Risks

- **Plugin install complexity:** TypeScript extension needs `openclaw.plugin.json` + compiled JS. If the build fails, the tool won't register. Mitigation: keep the plugin minimal (one file, no deps).
- **PII safety regression:** Relaxing the low-confidence gate could leak company names if not carefully gated. Mitigation: force `with_company = ""` at `confidence != "high"`, test explicitly.
- **Agent still doesn't call the tool:** Even with a named tool, the agent might not call it. Mitigation: the deterministic fallback (U2) is now good enough that the operator gets a useful message either way. Measure via merged-flow logs.

## Verification Scenarios

1. **Missed call, low confidence, CRM match:** Caller has low identity confidence but Attio finds a demo-request deal → draft says "I saw your demo request" (no company name) → operator sees segment + urgency in card
2. **Missed call, demo booked, demo in 45 min:** High confidence + demo booked + `startsInMinutes=45` → draft says "You're scheduled for a demo in 45 minutes" → card shows "⚠️ Urgency: demo in 45 min — consider calling back"
3. **Missed call, agent calls `submit_draft`:** Agent runs, calls `submit_draft(jobId, draft)` tool → webhook renders card with agent's draft → log "callback won"
4. **Missed call, agent doesn't call back (180s timeout):** Fallback timer fires → card with context-aware deterministic draft → log "fallback won"
5. **Missed call, no CRM, no calendar:** No CRM match, no calendar → generic "sorry we missed your call" (existing behavior preserved)

## Sources & Research

- Agent tool registration: `src/plugins/types.ts:2611` (`registerTool`), `docs/plugins/tool-plugins.md` (`defineToolPlugin`)
- Missed-call draft path: `webhook_server.py:3894` (`_crm_reply_message`), `:3888` (`_crm_reply_opening`)
- Deal segments: `webhook_server.py:3848` (`_DEAL_SEGMENT_STAGES`), `:3874` (`classify_deal_segment`)
- Calendar context: `webhook_server.py:3634` (`lookup_sales_calendar_context`) — returns `demoState`, `startsInMinutes`
- Context brief: `webhook_server.py:4462` (`build_inbound_context_brief`) — urgency flag goes after Segment line ~4528
- Low-confidence gate: `webhook_server.py:3905-3906` — PII safety invariant, relax without company names
- Origin: `docs/brainstorms/2026-06-25-context-aware-missed-call-drafts-requirements.md`