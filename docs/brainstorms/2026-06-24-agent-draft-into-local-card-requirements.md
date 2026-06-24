# Agent Draft into Local Card — Requirements

**Date:** 2026-06-24
**Status:** Draft
**Author:** Brainstorm session (ce-brainstorm)
**Supersedes:** None (extends the dual-delivery architecture from `2026-06-22-missed-call-enrichment-requirements.md`)

## Problem

The Dialpad webhook has two delivery paths that produce inconsistent operator messages for the same inbound event:

- **Path A (local card):** Rich Telegram card with approval buttons, inbound context brief (identity, CRM, calendar, comms, provenance), and a deterministic knowledge-backed draft. Fast (~1-2s) but the draft logic is hardcoded and can misfire (e.g. a cost question getting an availability answer because the QMD doc's first paragraph was a May 2024 disclaimer).
- **Path B (OpenClaw hook):** Plain summary routed to the AI agent, which produces its own answer with full model + tool access (QMD, Attio, Calendar). Smarter answer but no rich context, no approval buttons, no SMS draft UX.

When both fire to the same Telegram topic, the operator sees two different-looking messages. The dedup fix (commit `d9446c5`, deployed via service restart today) sets `deliver=false` on the hook when the local card targets the same topic, making the hook `context_only` — but that means the agent's smarter draft is suppressed entirely and the operator only sees the dumber deterministic draft.

## Outcome

One operator message per inbound event: the rich local card (Path A's UX) populated with the AI agent's draft (Path B's brains). The agent processes the inbound event with full tool access, produces a draft, and that draft becomes the SMS approval draft inside the rich card with inline buttons. The deterministic draft path survives as a fallback when the agent is slow or fails.

## Users

- **Operator (Martin/Sales):** Receives one coherent message per inbound SMS or missed call, with the best available draft and one-tap approval. No more choosing between a fast-but-wrong draft and a smart-but-contextless message.

## Success Criteria

- Operator sees exactly one message per inbound event (no duplicates, no separate hook message)
- The draft inside the rich card is the agent's answer (full model + tool access) in the common case
- If the agent doesn't return within 30s, the operator still sees a rich card with the deterministic draft (fallback)
- Approval buttons work on the merged card exactly as they do on today's local card
- No upstream OpenClaw repo changes required

## Approach: Agent callback with deterministic fallback (Option C)

### Mechanism

1. Webhook receives inbound Dialpad event, ACKs immediately (existing ACK-first pattern preserved)
2. Webhook sends the OpenClaw hook with `deliver=false` (agent runs but does NOT post to Telegram — verified: gateway sets `delivery.mode = "none"` when `deliver=false`, `src/gateway/server/hooks.ts:109-115`)
3. Hook payload includes a `callbackUrl` (loopback URL on the webhook server) and a `jobId` for correlation
4. The agent's prompt instructs it to POST its final draft answer to the callback URL via its tool access (the agent has Bash/HTTP tool access)
5. Webhook starts a 30s fallback timer for that `jobId`
6. **Callback arrives first:** webhook renders the rich card with the agent's draft, sends to Telegram, cancels the timer
7. **Timer fires first:** webhook renders the rich card with the deterministic draft (knowledge-backed / CRM-aware / calendar-aware, now with the stopword fix from today's commit), sends to Telegram. A late callback is logged but discarded.

### Why this approach

- **No upstream OpenClaw changes:** Uses the existing `/hooks/agent` endpoint. The `deliver=false` field is already honored by the gateway (verified in source + deployed image 0.9.18). The callback is just an HTTP POST the agent makes via existing tool access — no new gateway endpoint needed.
- **Guaranteed delivery:** The 30s fallback timer ensures the operator always sees *something*. The deterministic draft path (now fixed) is a genuine safety net, not a useless placeholder.
- **Best-of-both:** The agent's draft (gpt-5.5 with QMD + Attio + Calendar tools) is strictly better than the deterministic path in the common case. When the callback wins, the operator gets the smartest draft inside the richest UX.
- **Non-blocking:** The webhook handler returns ACK immediately. The wait happens in a background thread/coroutine, so Dialpad's webhook timeout is never at risk.

### Key risks

- **Agent instruction-following:** The agent must POST to the callback URL. If it doesn't (model error, tool failure, ignores instructions), the fallback timer covers it — but the operator gets the dumber draft. Flaky instruction-following degrades quality, not correctness.
- **Two draft paths remain:** The deterministic fallback must stay maintained. This is a feature (resilience) but means the merge isn't 100% "one brain" — it's "agent brain when fast enough, deterministic brain as fallback."
- **Callback endpoint security:** The new `/internal/draft-callback` endpoint on the webhook server needs auth (loopback-only binding + shared-secret token) to prevent external injection of fake drafts.

## Scope Boundaries

### In scope

- New `/internal/draft-callback` endpoint on the webhook server (loopback-only, token-auth)
- Correlation state: `pending_drafts` table in the existing SQLite dedupe DB (job ID → event metadata + timer)
- Hook payload changes: add `callbackUrl`, `jobId`, and a prompt suffix instructing the agent to call back
- Background timer logic: 30s fallback to deterministic draft
- Rich card rendering: accept either deterministic or agent draft as the SMS approval draft text
- Suppression of the agent's own Telegram output via `deliver=false` (already works, just needs the field sent)

### Deferred for later

- Telegram message editing (replace the deterministic draft in-place when the agent's arrives later) — adds complexity, requires Telegram message-edit API wiring, and the fallback-only design is simpler
- Sync draft API on OpenClaw (would require upstream changes — explicitly out of scope)
- Migrating the deterministic draft path to call the agent internally (would couple the webhook to the gateway's internal agent API — fragile)

### Outside this product's identity

- Removing the deterministic draft path entirely (it's the fallback safety net)
- Changing the OpenClaw hook protocol or gateway internals
- Building a new agent runtime or model routing

## Dependencies / Assumptions

- **Assumption (verified):** The OpenClaw gateway honors `deliver=false` — confirmed in `src/gateway/server/hooks.ts:109-115` (delivery mode `"none"` when `deliver=false`) and `src/gateway/hooks.ts:256-258` (`resolveHookDeliver` returns `raw !== false`). Deployed in AlphaClaw 0.9.18.
- **Assumption (unverified):** The agent reliably follows instructions to POST its draft to a callback URL. The 30s fallback timer covers failures, but the callback success rate determines whether the operator gets the smart draft or the dumb one. Needs measurement during implementation.
- **Dependency:** The existing SQLite dedupe DB (`scripts/sms_sqlite.py` or equivalent) is available for correlation state.
- **Dependency:** The webhook server can bind a loopback-only endpoint for callbacks (it already runs an HTTP server on a port).

## Open Questions (Resolved — see Decisions Resolved)

1. **Callback success rate:** Resolved — ship with 30s fallback + logging; tune from production data.
2. **Agent prompt design:** Resolved — inject callback URL + jobId into the hook message text.
3. **Callback payload shape:** Resolved — plain text, no structured metadata.
4. **Multiple drafts:** Resolved — one draft per event.

## Decisions Resolved

- **Merge direction:** Agent draft into local card (Path A UX + Path B brains), not rich context into hook, not single-path.
- **Latency:** Wait for the agent's draft (up to 30s) rather than sending a placeholder and streaming. One good message, no fast-but-dumb placeholder.
- **Draft retrieval:** Agent callback to the webhook server, not sync API (no upstream changes), not session file polling (too fragile).
- **Fallback:** Deterministic draft at 30s timeout. The deterministic path stays maintained as a safety net.
- **No upstream OpenClaw changes:** Confirmed feasible because `deliver=false` is already honored and the callback is agent-initiated via existing tools.
- **Success rate (Q1):** Ship with 30s fallback + logging. Log which path won (callback vs deterministic) on every event. Tune the timeout from production logs — no separate measurement phase.
- **Prompt design (Q2):** Inject the callback URL + jobId into the hook message text. Lowest friction; upgrade to a registered tool if agent instruction-following proves unreliable.
- **Callback payload shape (Q3):** Plain text. Agent POSTs its draft as a single string. No JSON, no metadata. Keeps the agent's natural prose output and avoids parsing failures.
- **Multiple drafts (Q4):** One draft per event. Matches the current single-approval-button UX. Multi-intent messages are rare; the operator can type a custom reply for those.

## Reference

- Debug investigation: today's session (2026-06-24), ce-debug skill
- Dedup fix: commit `d9446c5` (deployed via `dialpad-webhook.service` restart today)
- Knowledge draft fix: commit `e0dbcd6` (stopword + availability-disclaimer guard, deployed today)
- Gateway deliver handling: `src/gateway/server/hooks.ts:109-115`, `src/gateway/hooks.ts:256-258`, `429`
- Existing architecture map: `references/openclaw-integration.md` in the dialpad skill