# Requirements — S1: Wire the dead enrichment/draft paths in the Dialpad auto-responder

**Date:** 2026-06-18
**Scope tier:** Standard
**Source idea:** S1 in `docs/ideation/2026-06-18-dialpad-auto-response-enrichment.md`
**Component:** `scripts/webhook_server.py` (standalone Dialpad webhook server)

## Problem

The auto-responder already implements three richer draft modes — CRM-aware, calendar-aware, and QMD-knowledge — but every inbound SMS falls back to a generic template (`"thanks for reaching ShapeScale for Business Sales…"`). Two causes:

1. The context commands those modes call (`DIALPAD_CRM_CONTEXT_COMMAND`, `DIALPAD_CALENDAR_CONTEXT_COMMAND`, `DIALPAD_QMD_COMMAND`) are unset/unverified, so the modes fail closed.
2. Even with the commands set, the rich path is gated by `_high_confidence_sales_context_allowed()` (`webhook_server.py:2203-2210`): it only runs for a **known, high-confidence Dialpad contact**. The "super generic" complaint is mostly about *unknown* senders, who are routed to the generic fallback before Attio is ever consulted.

## Outcome

For any inbound SMS on the sales line, the operator-facing approval draft contains real context — Attio company/deal/stage/last-note, a relevant QMD/ShapeScale answer, and (when available) the person's upcoming demo time — instead of boilerplate. A wrong identity match is made visible to the operator, not hidden. Auto-send behavior is unchanged.

## In scope

1. **Attio CRM adapter** — a standalone CLI invoked as `<cmd> "<phone> <name> <company>"`, returning JSON `{usable, status, basis, summary, deal, stage, company, owner}` per the contract at `webhook_server.py:2174-2180, 2254-2275`. Looks up the Attio record by phone (primary) / name. Built as a reusable CLI so **S2** can call it as a cascade stage.
2. **QMD leg** — confirm `qmd` is reachable from the webhook's runtime environment, set `DIALPAD_QMD_COMMAND`, and fix PATH/HOME if the subprocess can't find it. Expected to be configuration + environment, not new code. Contract: `<cmd> search "<query>"` → stdout with `@@` snippet markers (`webhook_server.py:2043-2114`).
3. **Calendar adapter** — a standalone CLI invoked as `<cmd> "<name> <company> <deal> <timestamp>"`, returning `{usable, status, basis, summary|title, startsInMinutes}` per `webhook_server.py:2300-2314`. **Source order: Calendly primary** (matched by email), **Attio deal demo-date attribute as fallback** for phone-only inbound where no email is resolved.
4. **Un-gate the lookups into the draft lane** — run the CRM/QMD/calendar lookups in the operator-approval draft path for medium/unknown-confidence senders, not only the `identityConfidence == "high"` known-contact path. Generic fallback fires only when all applicable adapters return `usable == false`.
5. **Provenance line in the draft** — the operator-facing draft carries the match basis inline (e.g., `↳ Attio: +1415… → Acme Corp · stage: Demo Booked · matched on phone`) so the operator can sanity-check before approving.
6. **Wiring** — set the three env vars + any API secrets in `.env` / the host secret store, restart `dialpad-webhook.service`. `DIALPAD_QMD_COMMAND` must be an **absolute path** (`/home/art/.local/bin/qmd`) — see resolved finding 3.
7. **Move draft generation off the webhook ACK path** *(forced by the un-gate — see resolved finding 1)*. Today `create_proactive_reply_draft()` runs the up-to-3×8s lookups **inline before** `send_response(200)` (`webhook_server.py:3329` before `:3475`); un-gating makes that run on every inbound, blocking the ACK for up to 24s when Dialpad expects ~5s. Required change: ACK 200 immediately, generate + store the draft in a background thread, and make the work **idempotent** on the Dialpad event id so a Dialpad retry (triggered by a slow ACK) cannot create a duplicate draft or duplicate send. This is the delivery-correctness invariant for the change; auto-send behavior itself stays as-is (S4).

## Out of scope (downstream — must not conflict)

- **S2** — full phone-first identity resolver (Gmail/Granola, Apollo, reverse-phone, caching, confidence scoring). S1's Attio adapter is the reusable first stage of that cascade; S1 does **not** build the cascade or rewrite identity confidence.
- **S4** — confidence-gated auto-send. Auto-send remains exactly as today (gated). Un-gating affects only the human-approval draft lane.
- **S5** routing + Attio write-back · **S3** segmented customer/prospect templates · **S6** approval-DB eval loop.
- No rewrite of the Dialpad contact-lookup identity scorer; S1 bypasses the gate for the draft lane only.

## Key decisions

| Decision | Choice | Rationale |
|---|---|---|
| How far to reach past the identity gate | Un-gate lookups into the **human-approval draft lane** only | Surfaces context on most inbound now without acting unattended on a possibly-wrong match; keeps S2/S4 seams clean |
| Uncertain Attio match | **Enrich + label** with provenance inline; operator catches bad matches | Preserves human-in-the-loop; avoids silently dropping useful context |
| Calendar source | **Calendly primary, Attio deal demo-date fallback** | Calendly keys on email; Attio fallback covers phone-only inbound |
| Adapter form | Standalone CLIs, **no live-OpenClaw dependency** | The webhook is a standalone systemd service; must not depend on the gateway being up |
| Auto-send | **Unchanged** | Owned by S4 |

## Resolved de-risk findings (2026-06-18)

1. **RESOLVED — draft generation IS on the ACK path** (`webhook_server.py:3329` runs before `:3475`), up to 24s inline (3×8s timeouts), no threading. Dialpad expects ~5s and will time out → retry. Un-gating exposes this on every inbound, so the async refactor + idempotency is now **required scope (item 7)**, not an assumption.
2. **UNVERIFIABLE — Attio demo-date attribute not confirmed.** The claude.ai Attio MCP is returning **401** (token broken/expired); the standard `deals` object returned 0 rows, so ShapeScale's pipeline is likely a **custom object or a List**, not `deals`. First implementation step must discover, via the Attio REST API with a valid key: (a) the correct pipeline object/list slug, (b) the demo-date attribute slug for the calendar fallback. **Also: rotate the exposed Attio MCP token.**
3. **RESOLVED — `qmd` is not on the systemd service PATH.** Binary is at `/home/art/.local/bin/qmd` (→ `~/.bun/bin/qmd`); the unit's PATH is the nix-store systemd bin only. Fix is concrete: set `DIALPAD_QMD_COMMAND=/home/art/.local/bin/qmd` in `.env`. (Today it fails closed to `"unavailable"`, so no crash — just no knowledge.)

## Remaining assumptions (planning verifies)

4. **Attio search-by-phone hit rate** is unknown. If most inbound senders aren't in Attio, the CRM/calendar legs no-op for them and QMD carries enrichment — still an improvement over boilerplate.
5. The un-gate is a **code change** to mode-selection in `webhook_server.py` (run rich lookups in the draft branch; retain generic fallback when all unusable), not a pure config change.
6. A **dedicated Attio API key** is available for the webhook env (the adapter calls Attio REST directly, not via the broken/unreachable claude.ai Attio MCP).

## Success criteria

- A known Attio contact texting the sales line produces an operator draft containing real company/stage/last-note + a relevant QMD answer + a provenance line — verified on a real or replayed inbound message.
- An unknown sender asking a pricing/booking/product question gets a real QMD-grounded answer in the draft instead of the generic template.
- When all three adapters return unusable, the generic fallback still fires unchanged (no regression for true cold/unknown contacts with no question match).
- No change to auto-send behavior; the webhook does not gain a hard dependency on OpenClaw/AlphaClaw being up.

## Open questions for planning

- Exact insertion point for the un-gate in the mode-selection block (`webhook_server.py:2337-2393, 2504-2559`) that runs the rich lookups in the draft lane while preserving the existing high-confidence auto-path.
- Whether the three lookups should run concurrently to stay within a latency budget.
- Calendly API: which auth/credential and which endpoint resolves a scheduled event by invitee email.
- Where the provenance line is rendered so it reaches the operator (Telegram approval card) but is stripped from the customer-facing send text.
