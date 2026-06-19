---
module: scripts/webhook_server.py
problem_type: concurrency, idempotency, message-delivery
tags: [ack-first, idempotency, sqlite-wal, dialpad-webhook, replay-safety]
related_pr: "#92"
---

# ACK-first webhook + idempotency: the non-obvious traps

Hard-won learnings from PR2 (ACK-first async draft generation + SMS idempotency).
Three adversarial review rounds + Codex caught issues the first implementation
missed; these are the ones worth remembering.

## 1. Never replay user-visible output on a post-ACK failure

The tempting move — "on a post-ACK processing failure, release the dedupe claim
so a Dialpad retry can recover the message" — is **wrong**. By the time post-ACK
processing fails, a draft / OpenClaw hook / Telegram review card may already have
fired. Releasing the claim lets the retry **re-emit** them: duplicate operator
cards, potential duplicate customer SMS. The invariant (`~/CLAUDE.md` Product
Security Judgment) wins: **never replay user-visible output.** So a post-ACK
failure **keeps** the claim and accepts the rarer silent gap (message stored,
auto-draft maybe missing) — recoverable, and not customer-visible.

Corollary: release IS correct on a *pre-ACK* failure (storage error) — nothing
visible has been emitted yet, so a retry replay is safe and desirable.

## 2. Claim before storage, but mind the flip-side race

Claim the dedupe key **before** storage so a retry short-circuits before storage
re-fires (storage isn't fully idempotent — contact `message_count`, `received_at`).
Known residual (Codex): a concurrent retry within the first delivery's ~ms storage
window can ACK a duplicate before storage is durable. A complete fix needs a
two-phase pending/stored claim or idempotent storage — deliberately deferred, not
bolted onto a delivery-safety PR.

## 3. `PRAGMA journal_mode=WAL` must be best-effort, per connection

Setting WAL on **every** connection needs a brief header lock that **contends on
a fresh/hot DB** and can raise `OperationalError` *before* `busy_timeout`
serializes — which made the dedupe claim **fail open** under modest concurrency
(a flaky "2 winners" concurrency test, ~1/3 of full-suite runs). Fix:
`busy_timeout` is mandatory (it provides the serialization); WAL is wrapped in
`try/except` (`_apply_sqlite_concurrency_pragmas`). All writers sharing
`sms_approvals.db` must use the same helper or they drift.

## 4. A failed ACK write must not skip processing

If Dialpad disconnects mid-ACK, `_ack_webhook_200` raises. Wrap it: on failure,
still run post-ACK processing (side effects once) — Dialpad's retry then hits the
duplicate branch for a clean 200. Otherwise the message is stored+claimed but
never processed, and the retry is suppressed.

## 5. ACK-first changes the response contract — by design

An async webhook's 200 is a *receipt*, not a result. The SMS endpoint body
necessarily shrinks to `{status, stored, processing}`; `hook_*`/`auto_reply_*`
fields are computed after the ACK and can't be in it. Restore async observability
via a status record / job id if needed, not by faking synchronous fields.

## Meta-learning

The adversarial reviewer caught a HIGH bug introduced by a *previous* review
round's fix (the post-ACK rollback → replay). Re-review after each fix round; a
fix can introduce a worse bug than the one it closes.
