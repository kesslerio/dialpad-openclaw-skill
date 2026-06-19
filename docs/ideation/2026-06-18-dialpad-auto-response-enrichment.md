# Ideation — Dialpad inbound auto-response: enrichment, branching, routing

**Date:** 2026-06-18
**Subject:** Improve the Dialpad inbound-message auto-responder (AlphaClaw/OpenClaw) so it identifies the sender, branches customer-vs-prospect, drafts grounded replies, auto-sends at high confidence, and routes sales vs work.
**Mode:** repo-grounded against `~/projects/skills/work/dialpad/` + AlphaClaw state.
**Output:** markdown (no `docs/ideation/` HTML convention in this repo; markdown is grep-able and fits the rg/Obsidian workflow).

---

## The reframe (read this first)

Three findings from grounding overturn the premise of the request:

1. **The "super generic" problem is mostly a wiring gap, not a missing feature.** `webhook_server.py` already implements *three* draft modes: generic fallback, QMD-knowledge "rich reply," and a **CRM-aware / calendar-aware** draft. The richer modes **fail closed to the generic template** because `DIALPAD_CRM_CONTEXT_COMMAND`, `DIALPAD_CALENDAR_CONTEXT_COMMAND` are unset in `.env` and `DIALPAD_QMD_COMMAND` is unverified. You are not building enrichment from zero — you are filling in commands the code already calls.

2. **Apollo is the *last* enrichment step, not the first.** Apollo's match endpoint cannot enrich by phone number — it needs email or name+domain. Inbound SMS gives you only a phone. So "look up Apollo.io" can't lead the cascade. The phone→person wall is the real engineering problem, and the cheap/accurate signal lives in systems you already own (Attio, Gmail, Dialpad history, Granola), not Apollo.

3. **Your eval dataset already exists.** `sms_approvals.db` logs every generated draft and the operator's approve/edit/reject + final sent text. Generated-vs-sent diff is free labeled ground truth. "Have a subagent analyze the traces" → the asset isn't Opik (not wired), it's the approval log.

---

## Grounding (verified, with pointers)

- **Ingress:** standalone Python HTTP server, `webhook_server.py:3207` (`POST /webhook/dialpad`), port 8888, systemd `dialpad-webhook.service`. **Not** routed through OpenClaw — hooks disabled (`openclaw.json:1963` `"enabled": false`).
- **Storage:** `~/clawd/logs/sms.db` + `~/niemand/logs/sms.db` (messages, FTS5); `~/clawd/logs/sms_approvals.db` (drafts, status, fingerprint, metadata).
- **Draft modes:** generic fallback `webhook_server.py:1998-2012`; rich/QMD `:2434-2501`; CRM/calendar-aware `:2337-2393` (both context commands **unset** → fail closed).
- **Contact lookup:** `lookup_contact_enrichment() :460-520` — **Dialpad API only**. No Attio, Apollo, web, or email. No auto contact create on inbound.
- **Routing:** default → Telegram group `-1003882776023` (no topics); priority phones → Sales/Command Center group `-1003744039348` topic 2. No sales-vs-work split.
- **Send gate:** draft-for-review. Approval via Telegram buttons or `bin/approve_sms_draft.py` + operator token; bot/agent actors rejected. (`DIALPAD_AUTO_REPLY_ENABLED=1` governs draft creation, not unattended send.)
- **Telemetry:** no Opik traces flowing (`opik-trace-analytics` skill installed, not wired). Session jsonl + the two SQLite DBs + `/tmp/openclaw/openclaw-*.log`.
- **Volume:** ~5–10 inbound SMS/day; ~60–70% trigger a draft.

### API feasibility (the constraints that shape design)
- **Dialpad:** CONFIRMED — SMS event webhooks (`from_number`, `contact.id`, `contact.name`, `text`); `POST /api/v2/sms/send`; contact create `POST /api/v2/contacts` / update `PATCH .../{id}`. **No notes/metadata field on contacts** → canonical enrichment store must be Attio, not Dialpad. Company-wide 20 req/sec.
- **Apollo:** `POST /api/v1/people/match` by **email or name+domain only** — *no phone input*. Phone *reveal* is async (webhook, minutes), ~9+ credits (~$1.80+) per full enrich.
- **Reverse-phone (Numverify/Zyla/Searchbug):** cheap but **low accuracy on business/VOIP numbers** — last-resort tail, not a primary.

---

## Candidates generated → critiqued (28 → 6 survivors)

**Merged/rejected (with reasons):**
- *Reverse-phone & web-search as primary identification* — rejected as standalone: low hit rate on business numbers, noisy, costs credits for weak signal. Survives only as the cascade tail in **S2**.
- *Granola/meeting-history, last-touchpoint recency, per-segment templates, intent-classifier expansion* — folded into **S3** (all are "richer context for the branch").
- *Mirror inbound to Attio timeline* — folded into **S5** write-back.
- *Standalone Opik wiring* — demoted; the approval-DB eval (**S6**) is the higher-value version of "analyze the traces."
- *Enrichment cache, time-boxed auto-send, safe-intent allowlist* — sub-components folded into **S2**/**S4**.

The six survivors below cover all five axes.

---

## Survivors (ranked)

### S1 — Wire the dead enrichment paths *(do this first; hours, not weeks)*
**Axis:** generation plumbing. **Leverage: highest. Effort: lowest. Confidence: high.**
The CRM-aware and calendar-aware draft modes already exist and call out to `DIALPAD_CRM_CONTEXT_COMMAND` / `DIALPAD_CALENDAR_CONTEXT_COMMAND`; QMD rich-reply calls `DIALPAD_QMD_COMMAND`. All three are unset/unverified, so every richer draft collapses to "thanks for reaching out, we'll follow up." Write a thin `attio-context <phone|email>` CLI that returns the JSON shape the code expects, confirm `qmd` runs in the webhook's environment, set the env vars, restart the service. This alone removes most of the "generic" complaint before any new architecture exists. **Risk:** the existing context-command contract may be thin/underspecified — verify the expected JSON schema in `_run_context_command` before writing the adapter.

### S2 — Phone-first identity resolver with a cheap→expensive cascade and provenance
**Axis:** sender identification. **Leverage: high (it's the backbone). Effort: medium. Confidence: high.**
Design around the phone→person wall. Cascade, short-circuit on a confident hit, cache by phone (avoid re-paying Apollo):
1. Dialpad contact (already have it)
2. **Attio search by phone** (your CRM is the best first source)
3. **Gmail / Granola history** by phone or any associated email ("look up my email" — this is the cheap, high-signal step)
4. **Apollo match** — *only once an email/name is resolved* by 1–3
5. Reverse-phone / web search — last resort, flagged low-confidence
Emit `{identity, confidence, sources[]}` so downstream branching and the auto-send gate can reason about *how* the person was identified. **Corrects the original instinct that Apollo leads** — it's step 4, gated on steps 1–3 producing an email.

### S3 — Customer-vs-prospect branching off Attio deal state, answered from QMD
**Axis:** response generation. **Leverage: high (the headline feature). Effort: medium. Confidence: medium-high. Depends on S1+S2.**
Once identity resolves, branch on Attio deal stage → {customer, prospect-with-demo, prospect-cold, churned}, pull the **latest deal note**, classify the inbound intent, and answer the actual question from QMD/ShapeScale knowledge. A customer asking pricing gets a different draft than a cold prospect; someone who did a demo last week gets "following up from your demo." This is the visible payoff of S1+S2. **Risk:** template sprawl — keep segments to ~4 and let QMD carry the specifics rather than hand-writing every variant.

### S4 — Confidence-gated auto-send with a risk classifier + shadow-mode graduation
**Axis:** auto-send gating. **Leverage: high (it's the safety spine). Effort: medium. Confidence: medium.**
"Auto-send where high confidence" is only safe with an explicit gate: `confidence = f(identity_resolved, intent_classified, knowledge_match, no_risk_flags)`. A **risk classifier** hard-blocks pricing/legal/complaint/negotiation from *ever* auto-sending. Only deterministic safe intents (resend booking link, business hours, "got your message") are auto-send candidates — and only **after shadow mode**: run S1–S3 draft-only for a few weeks, measure operator edit rate per intent from `sms_approvals.db`, graduate an intent to auto-send when its edit rate drops below threshold. Without this, auto-send is reckless on your own sales line. **This is the gate that makes the user's auto-send ask defensible.**

### S5 — Route by Dialpad *line*, not content; write enrichment back to Attio
**Axis:** routing + contact write-back. **Leverage: medium-high. Effort: low-medium. Confidence: high. Semi-independent — can run in parallel with S2/S3.**
Two clean wins:
- **Routing:** key the sales-vs-work split on *which Dialpad number received the message* (sales line `+14155201316` vs your personal/work line), not on content classification. Deterministic, no misrouting. Sales line → sales topic + auto-responder; work line → work topic, responder off (or a different one). Create the two Telegram topics and map line→topic. This is the "create another Dialpad topic" idea, done the reliable way.
- **Write-back:** on enrichment, create/update the Dialpad contact (name/company/title) for caller-ID, but write the **canonical** enrichment + "inbound SMS received" event to **Attio** (Dialpad contacts have no metadata field). Attio becomes the source of truth; Dialpad just gets a readable name.

### S6 — Turn `sms_approvals.db` into an eval + weekly pulse
**Axis:** observability/feedback. **Leverage: medium (underpins everything). Effort: low-medium. Confidence: high. Stand up early.**
Every operator edit is labeled data: generated draft vs final sent text = quality signal. Build (a) an eval that computes edit distance + categorizes failure modes per intent, and (b) a weekly pulse: volume, auto-send rate, edit rate, top failing intents/segments. This is what tells you S1–S4 are working and *where* they fail, and it's the data that lets S4 graduate intents safely. Optionally wire the installed `opik-trace-analytics` skill for live tracing, but the approval DB is the cheaper, already-populated win.

---

## Suggested sequencing

| Order | Item | Why now |
|---|---|---|
| 1 | **S1** + S5 routing | Quick wins; S1 removes most of the perceived problem today; routing is deterministic and independent |
| 2 | **S6** | Stand up measurement before changing generation, so you can prove improvement |
| 3 | **S2** | The identity backbone everything richer depends on |
| 4 | **S3** | The headline branching feature, once identity + plumbing exist |
| 5 | **S4** | Graduate to auto-send only after S6 shows low edit rates |

## Riskiest assumptions to validate before planning
- The `_run_context_command` JSON contract (S1) — confirm the exact shape before writing the Attio adapter.
- Attio search-by-phone hit rate on inbound senders (S2) — if most senders aren't in Attio, S3's branch leans on Gmail/Apollo more than expected.
- That `qmd` is reachable from the webhook's systemd environment (S1) — PATH/HOME issues are likely given the AlphaClaw/systemd-user PATH history on this host.
