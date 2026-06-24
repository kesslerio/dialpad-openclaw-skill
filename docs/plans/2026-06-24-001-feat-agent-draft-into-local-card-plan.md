---
title: feat: agent draft into local card
type: feat
date: 2026-06-24
origin: docs/brainstorms/2026-06-24-agent-draft-into-local-card-requirements.md
---

# feat: Agent Draft into Local Card

## Summary

Merge the Dialpad webhook's two delivery paths into one operator message: the AI agent's draft (from the OpenClaw hook) becomes the SMS approval draft inside the rich local Telegram card. The webhook sends the hook with `deliver=false` (agent runs but doesn't post to Telegram), the agent POSTs its draft back to a new callback endpoint on the webhook server, and the webhook renders the rich card with that draft. A 30s fallback timer uses the deterministic draft if the callback doesn't arrive in time.

## Problem Frame

The current dual-delivery architecture produces inconsistent operator messages. Path A (local card) has the UX — rich card with approval buttons, inbound context brief, deterministic draft — but the draft logic is hardcoded and can misfire. Path B (OpenClaw hook) has the brains — full model + tool access (QMD, Attio, Calendar) — but produces a plain summary with no rich context, no approval buttons, no SMS draft UX. When both fire to the same Telegram topic, the operator sees two different-looking messages, or (after the dedup fix) only the dumber deterministic draft while the agent's smarter answer is suppressed.

The merged approach gives the operator one coherent message: the agent's draft rendered inside the rich card, with one-tap approval.

## Requirements

- R1. The operator sees exactly one message per inbound event — no duplicate delivery, no separate hook message.
- R2. The draft inside the rich card is the agent's answer (full model + tool access) in the common case.
- R3. If the agent doesn't return within 30s, the operator sees a rich card with the deterministic draft (fallback).
- R4. Approval buttons work on the merged card exactly as they do on today's local card.
- R5. No upstream OpenClaw repo changes required.
- R6. The callback endpoint is authenticated — the webhook binds `0.0.0.0`, not loopback-only.
- R7. A late callback (arriving after the fallback timer fired) is logged but discarded — no duplicate card.
- R8. The webhook logs which path won (callback vs deterministic) on every event for success-rate measurement.
- R9. The deterministic draft path stays maintained as the fallback safety net.

## Scope Boundaries

### In scope

- New `/internal/draft-callback` endpoint on the webhook server (token-authenticated)
- `pending_drafts` correlation table in the existing SQLite DB (job ID → event metadata)
- Hook payload changes: force `deliver=false` when the merged flow is active, append callback URL + jobId to the hook message
- 30s `threading.Timer` fallback to deterministic draft
- Rich card rendering: accept either deterministic or agent draft as the SMS approval draft text

### Deferred for later

- Telegram message editing (replace the deterministic draft in-place when the agent's arrives later)
- Sync draft API on OpenClaw (requires upstream changes — explicitly out of scope)
- Migrating the deterministic draft path to call the agent internally
- Structured callback payloads (draft + confidence + sources) — plain text only for now
- Multiple candidate drafts per event — one draft matches current UX

### Outside this product's identity

- Removing the deterministic draft path entirely (it's the fallback safety net)
- Changing the OpenClaw hook protocol or gateway internals
- Building a new agent runtime or model routing

---

## Key Technical Decisions

### KTD1: Agent callback, not sync API or session polling

The webhook retrieves the agent's draft via an HTTP callback: the hook message instructs the agent to POST its final answer to a callback URL on the webhook server. This avoids upstream OpenClaw changes (the existing `/hooks/agent` endpoint is used as-is with `deliver=false`) and avoids the fragility of polling session files. The callback is agent-initiated via the agent's existing tool/Bash access — no new gateway endpoint needed.

*(see origin: `docs/brainstorms/2026-06-24-agent-draft-into-local-card-requirements.md` — Decisions Resolved)*

### KTD2: 30s fallback timer with logging

A `threading.Timer(30, ...)` is scheduled after the hook is dispatched. If the callback arrives first, the timer is cancelled and the rich card renders with the agent's draft. If the timer fires first, the rich card renders with the deterministic draft. Every event logs which path won (callback vs fallback) for success-rate measurement. No separate measurement phase — production logs ARE the measurement.

### KTD3: Callback URL injected into hook message text

The callback URL and jobId are appended to the hook `message` field (in `format_hook_message`). This is the simplest approach — the agent already reads the message text. If agent instruction-following proves unreliable (measured via KTD2's logging), upgrade to a registered tool later.

### KTD4: Plain text callback payload

The agent POSTs its draft as a single plain-text string. No JSON, no metadata. The agent's natural output is prose; forcing JSON degrades answer quality and adds parsing failure surface. The rich card already shows inbound context (CRM, calendar, identity) — the draft doesn't need to repeat it.

### KTD5: Shared-secret auth on the callback endpoint

The webhook server binds `0.0.0.0:8081` (not loopback-only), so the `/internal/draft-callback` endpoint requires authentication. A shared-secret token checked via `hmac.compare_digest` (matching the existing Telegram webhook auth pattern at `webhook_server.py:5137-5149`). The token is generated per-job and included in the callback URL — so the agent doesn't need to know a static secret, it just POSTs to the URL it was given.

---

## High-Level Technical Design

```
  Dialpad webhook event
          |
          v
  +------------------+
  | ACK 200 (immediate) |
  +------------------+
          |
          v
  +------------------+
  | Build deterministic draft (existing path) |
  | Store as fallback in pending_drafts[jobId] |
  +------------------+
          |
          v
  +------------------+
  | Send hook with deliver=false |
  | Message includes callback URL + jobId |
  +------------------+
          |
          v
  +------------------+
  | Start threading.Timer(30s) |
  +------------------+
          |
          |     +------------------+
          |     | Agent runs (deliver=false) |
          |     | POSTs draft to callback URL |
          |     +------------------+
          |               |
          v               v
  +------------------+   +------------------+
  | Timer fires first |   | Callback arrives first |
  | Render card with  |   | Cancel timer |
  | deterministic     |   | Render card with |
  | draft             |   | agent draft |
  +------------------+   +------------------+
          |               |
          v               v
  +------------------+
  | Send rich card to Telegram (one message) |
  | Log which path won |
  +------------------+
```

Race condition handling: the `pending_drafts` row has a `status` column (`waiting` → `delivered`). Both the timer callback and the HTTP callback handler attempt a `UPDATE ... SET status='delivered' WHERE status='waiting'` — the first to succeed wins, the other is a no-op (late callback logged and discarded).

---

## Implementation Units

### U1: pending_drafts SQLite table

**Files:** `scripts/webhook_server.py`

Add a `pending_drafts` table to the existing SQLite dedupe DB (`_sms_dedupe_db_path()` at `:1624`).

- New constant `PENDING_DRAFTS_TABLE = "pending_agent_drafts"`
- New `_init_pending_drafts_db(db_path=None)` function, reusing `_apply_sqlite_concurrency_pragmas` (`:1527`) and `CREATE TABLE IF NOT EXISTS`
- Schema: `(job_id TEXT PRIMARY KEY, created_at_ms INTEGER, status TEXT DEFAULT 'waiting', event_json TEXT, fallback_draft TEXT, callback_token TEXT)`
- New helper functions:
  - `insert_pending_draft(job_id, event_json, fallback_draft, callback_token)` — called after the deterministic draft is built
  - `claim_pending_draft(job_id)` — `UPDATE ... SET status='delivered' WHERE status='waiting'` returning the row; returns `None` if already claimed
  - `get_pending_draft(job_id)` — read-only lookup (for the timer callback)

**Tests:** `tests/test_sender_enrichment.py`

- `test_pending_drafts_insert_and_claim` — insert, claim returns the row, second claim returns None
- `test_pending_drafts_late_callback_after_timer` — timer claims first, callback gets None, no double-delivery
- `test_pending_drafts_retention` — rows older than retention window are pruned

### U2: /internal/draft-callback endpoint

**Files:** `scripts/webhook_server.py`

Add a new route in `do_POST` (`:5055`):

- `if self.path == "/internal/draft-callback": self.handle_draft_callback(); return`
- New `handle_draft_callback` method:
  - Read JSON body via `read_json_body("draft-callback")` (`:5115`)
  - Extract `jobId` and `draft` (plain text string) from the body
  - Validate `jobId` exists in `pending_drafts` and `status == 'waiting'`
  - Validate `draft` is a non-empty string under 1000 chars
  - `claim_pending_draft(job_id)` — if claimed, render the rich card with the agent's draft, send to Telegram, log "callback won"
  - If already `delivered` (timer won), log "callback lost (timer already fired)" and return 200
- Auth: extract `jobId` from the body, look up the `callback_token` in `pending_drafts`, compare with the `X-Callback-Token` header via `hmac.compare_digest`. Reject 401 if missing/mismatched.

**Tests:** `tests/test_sender_enrichment.py`

- `test_draft_callback_renders_card_with_agent_draft` — POST callback with valid jobId + draft, assert Telegram card sent with agent draft text
- `test_draft_callback_rejects_missing_token` — POST without X-Callback-Token header, assert 401
- `test_draft_callback_rejects_wrong_token` — POST with wrong token, assert 401
- `test_draft_callback_after_timer_fired` — timer already claimed, POST callback, assert 200 + "lost" log, no double card
- `test_draft_callback_rejects_oversized_draft` — draft > 1000 chars, assert 400
- `test_draft_callback_rejects_unknown_jobid` — jobId not in pending_drafts, assert 404

### U3: Hook message + payload changes

**Files:** `scripts/webhook_server.py`

Modify `format_hook_message` (`:4867`) to append the callback instruction when the merged flow is active:

- Add a `callback_url` and `job_id` parameter (optional, defaults to None)
- When both are present, append to the message text: `"\n\nReply-Draft Callback: POST your final answer (plain text only) to <callback_url> with header X-Callback-Token: <token>. Include jobId: <job_id> in the JSON body as {\"jobId\": \"<job_id>\", \"draft\": \"<your answer>\"}."`
- When not present (backward compat), the message is unchanged

Modify `build_openclaw_hook_payload` (`:4906`):

- Accept `callback_url`, `job_id`, `callback_token` from `normalized_event` (set by the handler when the merged flow is active)
- Pass them to `format_hook_message`
- Force `deliver=False` when the merged flow is active (override `operator_notification` — the agent must not post to Telegram because the webhook will render the card)

**Tests:** `tests/test_sender_enrichment.py`

- `test_hook_message_includes_callback_url_when_merged_flow_active` — assert callback URL + jobId appear in the hook message
- `test_hook_message_unchanged_when_no_callback` — backward compat, no callback params → message unchanged
- `test_hook_payload_deliver_false_when_merged_flow_active` — assert `deliver=False` even if `operator_notification` would normally set it True
- `test_hook_payload_deliver_respects_operator_notification_when_no_merge` — backward compat

### U4: 30s fallback timer + merged card rendering

**Files:** `scripts/webhook_server.py`

In `_process_inbound_post_ack` (`:5307`) and the missed-call handler (`:5707`):

- After building the deterministic draft (the existing `auto_reply_message`), insert a `pending_drafts` row with the fallback draft + a generated `callback_token`
- Build the callback URL: `http://127.0.0.1:{PORT}/internal/draft-callback`
- Set `normalized_event["callback_url"]`, `["callback_job_id"]`, `["callback_token"]` so U3's payload builder picks them up
- Send the hook (now with `deliver=false` + callback URL in the message)
- Start `threading.Timer(30, _fallback_timer_callback, args=(job_id,))` — the timer callback:
  - `claim_pending_draft(job_id)` — if claimed, render the rich card with the deterministic fallback draft, send to Telegram, log "fallback won"
  - If already claimed (callback won), do nothing
- The callback handler (U2) and the timer callback both call a shared `_render_merged_card(job_id, draft_text, event_json)` function that builds the rich card (header + brief + approval review suffix with the draft + buttons) and sends it to Telegram

**Tests:** `tests/test_sender_enrichment.py`

- `test_merged_flow_callback_wins` — callback arrives within 30s, card sent with agent draft, timer cancelled, log "callback won"
- `test_merged_flow_timer_wins` — no callback, timer fires, card sent with deterministic draft, log "fallback won"
- `test_merged_flow_no_double_card` — callback arrives after timer fired, no second card sent
- `test_merged_flow_card_has_approval_buttons` — the merged card has the same reply_markup as the current local card
- `test_merged_flow_missed_call` — same flow for the missed-call path

### U5: Feature flag + logging

**Files:** `scripts/webhook_server.py`, `.env`

- New env var `DIALPAD_MERGED_DRAFT_FLOW` (bool, default False) — gates the merged flow. When False, the existing dual-delivery path runs unchanged (backward compat).
- New env var `DIALPAD_AGENT_DRAFT_TIMEOUT_SECONDS` (int, default 30) — configurable timeout.
- Structured log line on every merged-flow event: `[merged-flow] job_id=X path={callback|fallback} elapsed_ms=N draft_chars=N`

**Tests:** `tests/test_sender_enrichment.py`

- `test_merged_flow_disabled_by_default` — no pending_drafts row, no timer, existing dual-delivery path runs
- `test_merged_flow_enabled_flag` — env var set, assert pending_drafts row created + timer started
- `test_merged_flow_custom_timeout` — `DIALPAD_AGENT_DRAFT_TIMEOUT_SECONDS=5`, assert timer fires at 5s

---

## System-Wide Impact

- **Operators:** One coherent message per inbound event instead of two inconsistent ones. Slight delay (up to 30s) before the card appears, but the draft is smarter.
- **Dialpad:** No change — the webhook ACKs immediately, the merged flow happens post-ACK.
- **OpenClaw gateway:** No change — the hook endpoint is used as-is with `deliver=false` (already supported).
- **Agent (niemand-work):** Receives a hook message with callback instructions. Must POST its answer to the callback URL. No agent config change needed — the instruction is in the message text.

## Dependencies

- `deliver=false` honored by the gateway — verified in `src/gateway/server/hooks.ts:109-115` (deployed in AlphaClaw 0.9.18)
- Agent has HTTP/Bash tool access to POST to the callback URL — assumed (the agent already uses tools)
- Agent instruction-following reliability — unverified; measured via KTD2's logging

## Risks

- **Agent doesn't call back:** The 30s fallback covers it, but operators get the dumber draft. Mitigation: log success rate, tune timeout, upgrade to a registered tool if flaky.
- **Race condition:** Timer and callback both try to render the card. Mitigation: SQLite `UPDATE ... WHERE status='waiting'` atomic claim — first wins, second is a no-op.
- **Callback endpoint abuse:** The server binds `0.0.0.0`. Mitigation: per-job callback token in the URL, `hmac.compare_digest` auth, validate jobId + draft length.
- **Latency regression:** Operators wait up to 30s instead of seeing the card in 1-2s. Mitigation: configurable timeout, log elapsed time, can tune down if the callback success rate is high.

## Verification Scenarios

1. **Pricing SMS, callback wins:** Inbound "I want to know cost please" → agent POSTs pricing draft within 30s → card shows agent's pricing answer → operator approves → SMS sent.
2. **Pricing SMS, fallback wins:** Same inbound → agent doesn't call back within 30s → card shows deterministic pricing draft → operator approves → SMS sent.
3. **Missed call, callback wins:** Inbound missed call → agent POSTs followup draft → card shows agent's draft with missed-call context.
4. **Late callback discarded:** Timer fires at 30s → card sent with deterministic draft → agent calls back at 35s → log "callback lost" → no second card.
5. **Feature flag off:** `DIALPAD_MERGED_DRAFT_FLOW` not set → existing dual-delivery path runs unchanged → no pending_drafts, no timer, no callback endpoint hit.
6. **Callback auth rejected:** POST to `/internal/draft-callback` without/with-wrong token → 401 → no card rendered.

## Sources & Research

- Debug investigation (this session): confirmed dual-delivery architecture, dedup fix, knowledge draft misfire
- Gateway `deliver=false` handling: `src/gateway/server/hooks.ts:109-115`, `src/gateway/hooks.ts:256-258`
- Existing webhook integration seams: SQLite dedupe DB (`webhook_server.py:1624`), HTTP routing (`:5055`), hook payload (`:4906`), card rendering (`:5307`, `:5707`), threading (`:5996`)
- Origin document: `docs/brainstorms/2026-06-24-agent-draft-into-local-card-requirements.md`