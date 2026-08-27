---
title: Hosted-Postgres Migration — Remote Dialpad Access
type: feat
date: 2026-08-27
readiness: "awaiting-operator-sign-off"
scope: docs-only-proposal
triggered_by: operator question — "keep dialpad DB local vs Supabase/Convex for mobile/other-machine access"
depends_on:
  - operator decision on DB hosting preference (open question O1)
  - operator decision on attacker model for remote send (open question O2)
---

# Hosted-Postgres Migration — Remote Dialpad Access

**(Draft for sign-off — no repo changes made. Blocked on two operator decisions, see §Open Questions.)**

Bottom line: the call/SMS/correspondence store is currently a **local SQLite file** on `theshop`. Keeping it there is the right choice *today*; a hosted Postgres (Supabase/Convex or self-hosted) becomes worthwhile only if mobile/other-machine access grows into **team use, analytics, backups-as-a-service, or loss-of-this-host risk**. Before writing anything, confirm (O1) the **desired DB hosting** and (O2) the **attacker model** for remote send. Those decisions are yours; implementation is paused so you can veto cleanly, not suppressed behind an ill-formed plan.

---

## Overview

Right now:
- One **SQLite** file on `theshop`. Producer = `scripts/webhook_server.py` (`systemd dialpad-webhook.service`), the **sole writer**, ingesting DialPad SMS/webhooks and persisting via `scripts/sms_sqlite.py` (`store_message`, thread getters, `search_messages` using the FTS index `messages_fts`, `cleanup_stale_contacts`) and SMS attribution/approval in `scripts/sms_approval.py` (`create_draft`, `get_draft`, `approve_draft`, `invalidate_pending`, `record_agent_direct_send` — kept in **one transaction**), with denormalized contact summaries. Analysts/operators read via `bin/*` CLIs.
- Call history and live attributes already come from the **DialPad API** (`run_generated_json`, driven by `_dialpad_compat.py`), not the local store — so transcripts/live-data need **no** migration.
- Prod paths: store default `DIALPAD_SMS_DB=/home/art/niemand/logs/sms.db`; approval default `/home/art/niemand/logs/sms_approvals.db` (both env-overridable). Secrets: DialDrive + Twilio keys, webhook secrets, Telegram/Bot tokens, OAuth creds — all currently in `.env(600)`.

Why stay put meanwhile:
- Local SQLite = **zero vendor, no network dependency, full control of regulation-sensitive PII** (lead numbers, bodies, corp correspondence), easy backups/version control. Perfect for a single-operator, on-box flow.
- Prematurely adopting Supabase/Convex adds a network hop + SLA + third-party PII exposure for benefits not yet earned.

When to revisit — any single of these tips toward a hosted store:
1. **Two+ people write/serve from different machines** (WAL handles reader bursts + one writer; two publishers break the single-writer model).
2. **Managed durability**: automatic snapshots/point-in-time-recovery + HA.
3. **Analytics/BI**: dashboards over the call+SMS corpus — best as a read-only replica, keeping SQLite the source.
4. **Loss-of-this-host risk**: `theshop` dies → you lose history.

## Requirements

**Functional**
- **F1 (parity):** Whatever we change, `bin/*` JSON contracts and SQLite-produced rows stay equivalent until an explicit, tested cutover. No silent behavior drift.
- **F2 (producer stays one writer):** The hosted store is written only by theeshop's webhook; laptops are readers + *intent originators*, never independent writers to shared state.
- **F3 (remote browse):** Private-net/device clients can list threads, view a thread, and full-text search the corpus.
- **F4 (remote send, not remote write):** A laptop drafts/replies, but the actual send is orchestrated by theeshop. See "Send path" below.

**Non-functional**
- **NF1 (encryption):** In transit (TLS, `PGSSLMODE=require` for managed hosts) and at rest (host-managed or KMS-wrapped keys). Secrets stay secrets.
- **NF2 (network isolation):** Never internet-open. `Tailnet`-bound; managed PG restricted by IP allow-list. If a managed DB forces any public ingress, disable and revert.
- **NF3 (audit + consent):** Remote reads must not weaken consent/legal logging. Corporate and lead contacts may be regulated — remote viewers obey **data minimization** (fields needed to do the job, nothing more) and the remote UI hides non-essential PII.
- **NF4 (revert):** Cut-over behind an env flag so rollback is instant (see Rollback).

## Approaches evaluated (short)

- **A — thin JSON read API on theeshop, Tailnet-only (recommended if staying on-box).** Simplest, smallest blast radius. Remote devices hit a read-only JSON endpoint; the live store never leaves `theshop`. Sends stay webhook-driven.
- **B — managed Postgres (Supabase/Convex) + GraphQL edge-function gateway.** Best fit if you want hosted durability + a hosted query surface. Bigger surface + third-party PII exposure. GraphQL via edge functions keeps writes funneling through one governed layer.
- **C — self-hosted DB + app-metadata/GraphQL gateway.** Gives B's ergonomics without a tenant, at the cost of you owning uptime/backups/HA.

Selection rests on **O1**. All three keep the "single writer = theeshop" rule.

## Send path (important correction)

Do **not** have laptops write SMS via the DB. Remote reply = laptop sends **draft intent** (`submit_draft`), routed to theeshop over the private net / callback token; theeshop **still authorizes, enriches, and notifies** (Telegram + audit). This preserves one governance choke point — no new permission, no second writer. The remote surface is therefore read-heavy; the lone write path stays inside the webhook.

## Design (if O1 = a hosted Postgres)

- **Abstract the store.** Split `scripts/sms_sqlite.py` responsibilities into a pluggable store; provide a `PostgresBackend` alongside the existing SQLite one. Flip via env `STORE_BACKEND`=sqlite|postgres; keep SQLite default (safe incremental cutover). Bad `PG_DSN` → **fail at boot**, matching the "loads before send-before-load safety" rule.
- **Model ports:** `messages` (+ full-text index), `contacts`, denormalized contact summaries, `sms_approval_drafts`. Preserve the **unique-key + retry/conflict idempotency** invariants exactly (they guard duplicate drafts/sends), so the Postgres path must honor the same unique/`ON CONFLICT` semantics.
- **Seamline:** every SQLite-touching path in `scripts/sms_sqlite.py`, `scripts/webhook_sqlite.py`, and the approval flow is the seam. `bin/*` live-attribute callsights (`run_generated_json`) are unchanged.

## Migration / cut-over / deployment

Ordered steps, each reversible:
1. Add abstraction + Postgres backend, **run side-by-side with SQLite, parity-tested.**
2. Cut over the webhook producer to Postgres behind the env toggle; validate **parity** against the prior SQLite dumps, don't assume.
3. Onboard the remote read surface (A) or GraphQL gateway (B/C).
4. Keep a nightly SQLite dump + managed backup/pitr as a **quick-revert fallback**.
5. Hypercare: watch parity + new error classes for ~1-2 weeks; only then treat SQLite as retired.

## Rollback

Env-only downgrade: `STORE_BACKEND=sqlite`. The webhook falls back to SQLite instantly, no code change. This is the single most valuable affordance of the abstraction.

## Observability & reliability

- Track Postgres query latency/error rates, pool saturation, lock-wait/deadlocks, WAL size, and webhook throughput; set thresholds/alerts on the **new** failure classes (network drops, auth TTL, lock conflicts), not just on SQLite equivalents.
- Keep parity with SQLite behaviors (denormalized contact summaries, `messages_fts` parity — either match its result set or consciously accept a Postgres full-text-index deviation).

## Effort & rough timeline (coarse)

Capacity band, not a commitment:
- **Spike / abstraction + Postgres backend + parity tests** — moderate; this is the load-bearing chunk.
- **Producer cut-over + validation** — short, logged, gated; a brief, monitored window.
- **Remote read surface (A/B/C)** — depends on choice: A is quickest; GraphQL gateway heaviest.
- **Secrets/backup + hypercare** — steady, ongoing.

## Open Questions for sign-off

- **O1 — Desired DB hosting?** A) Stay on-box (thin JSON API). B) Supabase/Convex. C) Self-hosted Postgres. Drives the whole seam + secret-handling design.
- **O2 — Attacker model for remote send?** Who is allowed to trigger `submit_draft` from off-box; does it need mfa / ip allow-list, or is private-net sufficient? Keeps IAM/notification-layer sizing right.

## Resources / assumptions

**Assumed (to confirm):** `theshop` runs continuously, is Tailscale-reachable from your devices, and the current single-producer/single-writer shape is unchanged. Call/live attributes come from the DialPad API and need no migration.
**Resources used:** current in-box SQLite store in `scripts/sms_sqlite.py` / `scripts/sms_approval.py`; prod path `/home/art/niemand/logs/sms.db`; `bin/*` live-attribute callsights; the current webhook + `submit_draft`/`X-Callback-Token` auth.

## Sources & prior work

- Prior migration-style plans in `docs/plans/` (esp. `2026-07-06-001-...` merged-flow/callback plan and `2026-05-14-001-...sms-history-visibility` plan) for store seams, concurrency, and cut-over cadence.
- Operator's shift in stance between rounds: stayed local until access/robustness needs grow; chose a design that is "local today, hosted-ready behind a toggle."

## Requirements traceability

- F1,F2,F4 → abstraction, single-writer, `STORE_BACKEND` cut-over (implemented by U-series).
- F3,A,B,C → remote read surface.
- O1,O2 → A/B/C + IAM/notification-layer sizing.

---

## Sign-off

Awaiting operator on **O1 (DB hosting)** and **O2 (attacker model)**. Once answered, Codex will review the realized plan; implementation resumes only after sign-off. Codex's findings should be addressed or acknowledged (👍) before any merge — do not squash-merge Codex without responding to them.
