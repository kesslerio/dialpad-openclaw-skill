---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
title: "Fix Dialpad Missed Call Deduplication (Issue #132), OpenClaw Hook Headers, and History Lookup"
created_at: "2026-09-04T02:45:00Z"
---

# Dialpad Webhook Server Improvements: Issue #132 Dedupe, Hook Attribution, and History Fix

## Goal Capsule

- **Objective**: Prevent duplicate Telegram alerts for multi-leg missed calls (Issue #132), eliminate HTTP 403 `proxy_attribution_required` errors on OpenClaw stock `/hooks/agent`, and resolve HTTP 400 Bad Request failures during Dialpad call history backfill.
- **Means**:
  1. Add a 60-second caller+line burst deduplication key in `claim_missed_call_notification` alongside root call ID deduplication.
  2. Supply `X-Forwarded-For` proxy client attribution headers in `send_to_openclaw_hooks` satisfying stock OpenClaw gateway ingress security requirements.
  3. Cap `started_before` to `now_ms` in `_fetch_recent_calls_around` to prevent Dialpad API 400 future-timestamp rejection, and avoid invoking history lookups when caller and line are already known from the webhook payload.
- **Stop Conditions**: All automated unit and integration tests pass in `tests/test_webhook_server.py` and `tests/test_webhook_hooks.py`; backward compatibility with existing dedupe keys and ACK-first idempotency invariants is preserved.

---

## Product Contract

### Summary
Deduplicate multi-leg Dialpad missed-call notifications within a 60-second burst window per caller and line, supply required proxy client attribution headers to stock OpenClaw gateway hooks, fix Dialpad API v2 call history datetime query formatting, and add comprehensive unit test suites.

### Problem Frame
Production monitoring of `dialpad-webhook.service` on `theshop` revealed three distinct recurring defects:
1. **Issue #132**: For calls routed across department legs or ring groups (such as the Sales line `(415) 520-1316`), Dialpad omits `entry_point_call_id` and provides unique `call_id`s on each leg. Because `build_missed_call_dedupe_key` claims `missed-call:root:<call_id>` for any non-empty `call_id`, each leg bypasses the fallback time-bucket deduplication and delivers separate duplicate cards to Telegram (e.g. 3 alerts in 16 seconds for caller `+19515518711`).
2. **OpenClaw Stock Hook 403 Rejection**: Stock OpenClaw gateway classifies incoming connections from the Docker bridge (`172.17.0.1`) as trusted proxy connections. When `send_to_openclaw_hooks` dispatches requests without `X-Forwarded-For`, OpenClaw marks the request as `unattributable-proxy` and rejects it with HTTP 403 (`proxy_attribution_required`).
3. **Dialpad History Lookup 400 Error**: Whenever `resolve_missed_call_context` queries Dialpad API v2 for call history, `_fetch_recent_calls_around` sets `started_before = event_ts_ms + 30m`. Because live webhooks arrive in real-time, `event_ts_ms + 30m` is in the future, which Dialpad rejects with HTTP 400 (`Timestamp range cannot be in the future.`). Furthermore, `handle_call_webhook` attempts a second context resolution even when caller and line are already resolved from payload fields.

### Requirements

- **R1 (Multi-leg Missed Call Deduplication)**: When a missed call webhook arrives, the server must atomically evaluate both the specific call/root ID AND a 60-second caller+line burst window (`missed-call:burst:<caller>:<line>`). If any notification for that caller and line was claimed within the last 60 seconds, subsequent legs must be identified as duplicates and suppressed before Telegram delivery.
- **R2 (OpenClaw Stock Hook Proxy Attribution)**: Requests to OpenClaw hooks (`http://127.0.0.1:18789/hooks/agent`) must include valid proxy client attribution (`X-Forwarded-For`) using the host's routable IP (or `OPENCLAW_HOOKS_CLIENT_IP` env override) so the stock gateway attributes the request to a trusted proxy without returning HTTP 403 `proxy_attribution_required`.
- **R3 (Dialpad History Query Compliance)**: `_fetch_recent_calls_around` must cap `started_before` to `min(current_now_ms, event_ts_ms + window_ms)` so query timestamps are never in the future.
- **R4 (Skip Unnecessary API Lookups)**: `handle_call_webhook` and `resolve_missed_call_context` must bypass network API calls to Dialpad when caller and line have already been resolved (`caller_path != "unresolved"` and `line_path != "unresolved"`).
- **R5 (ACK-First Idempotency Safety)**: All changes must preserve the rules in `docs/solutions/ack-first-webhook-idempotency.md`: never release claims on post-ACK failures, keep SQLite WAL concurrency pragmas (`busy_timeout=5000`), and maintain backward compatibility for existing callers of `build_missed_call_dedupe_key`.

### Success Criteria
- [ ] Multiple legs of a missed call arriving within 60 seconds with identical caller and line number result in exactly 1 claim and N-1 duplicate suppressions.
- [ ] Distinct calls from the same caller after the 60-second window expires are not suppressed.
- [ ] Concurrent or retried calls with identical `call_id` or `entry_point_call_id` remain suppressed regardless of timing.
- [ ] `send_to_openclaw_hooks` emits `X-Forwarded-For` and passes OpenClaw gateway ingress attribution without 403 rejections.
- [ ] `_fetch_recent_calls_around` returns HTTP 200 from Dialpad API v2 without 400 Bad Request future-timestamp errors.
- [ ] Full test suite passes via `pytest`.

### Scope Boundaries
- **In Scope**:
  - `scripts/webhook_server.py`: deduplication logic, hook forwarder headers, history fetcher timestamp bounds, and resolution guards.
  - `tests/test_webhook_server.py` and `tests/test_webhook_hooks.py`: unit tests for burst deduplication, hook client headers, and history query constraints.
- **Out of Scope**:
  - Reconfiguring OpenClaw Docker container networking or modifying OpenClaw gateway source code.
  - Modifying Dialpad's upstream webhook payload schema.
  - Modifying the SQLite database schema in `sms_approvals.db` (existing `missed_call_dedupe` table supports text keys and integer timestamps).

---

## Planning Contract

### Key Technical Decisions

- **KTD1 (Caller+Line 60s Sliding Burst Claim)**:
  - Rather than relying solely on integer division buckets (`timestamp // 60000`) which risk boundary splits at second 59 vs 00, `claim_missed_call_notification` will evaluate a burst key `missed-call:burst:<caller>:<line>`.
  - Inside the atomic SQLite transaction on `missed_call_dedupe`, it checks whether a row with `missed-call:burst:<caller>:<line>` exists with `timestamp_ms - first_seen_at_ms < 60_000`.
  - If a row exists within 60s: update `last_seen_at_ms`, increment `duplicate_count`, and mark as duplicate leg (`status: "burst_duplicate"`).
  - If no row exists or the existing row is $\ge$ 60s old: update/insert the burst key with `first_seen_at_ms = timestamp_ms`, claim the primary `missed-call:root:<call_id>` key, and return `claimed: True`.
  - If caller is unknown, burst deduplication is skipped to prevent cross-caller suppression.

- **KTD2 (Host Client IP Attribution for Hooks)**:
  - Stock OpenClaw gateway inspects `X-Forwarded-For` right-to-left for the first non-loopback, non-trusted hop.
  - `send_to_openclaw_hooks` will determine the outbound client IP via `OPENCLAW_HOOKS_CLIENT_IP` environment variable, falling back to dynamic UDP socket inspection (`s.connect(("192.168.4.1", 80))`) or `10.0.0.1` default.
  - Add `headers["X-Forwarded-For"] = client_ip` to `urllib.request.Request`.

- **KTD3 (Cap `started_before` and Guard Resolution)**:
  - In `_fetch_recent_calls_around`, set `started_before = str(min(int(time.time() * 1000), event_ts_ms + window_ms))`. Also omit `limit` or keep within Dialpad API v2 pagination guidelines.
  - In `handle_call_webhook`, after ACK-200, only invoke `resolve_missed_call_context(data)` with the real history fetcher if `resolved["caller_resolution_path"] == "unresolved"` or `resolved["line_resolution_path"] == "unresolved"`.

### High-Level Technical Design

```
                     Dialpad Inbound Missed Call Webhook
                                      │
                                      ▼
                        POST /webhook/dialpad/call
                                      │
           ┌──────────────────────────┴──────────────────────────┐
           ▼                                                     ▼
Pre-ACK Quick Resolution                              Dedupe Check (Atomic DB)
(stubbed history_fetcher)                             - Primary: root/call_id
           │                                          - Burst: caller+line (<60s)
           ▼                                                     │
    Is Duplicate? ────────────────────────── Yes ────────► Return 200 Suppressed
           │ No
           ▼
     ACK Webhook 200
           │
           ▼
Needs History Resolution? ──── No ───┐
(caller or line unresolved)          │
           │ Yes                     │
           ▼                         │
Dialpad API History Query            │
(started_before <= now_ms)           │
           │                         │
           ▼                         ▼
   Forward to OpenClaw ◄─────────────┘
  (/hooks/agent with X-Forwarded-For)
           │
           ▼
Telegram Notification / Merged Flow
```

---

## Implementation Units

### [U1] Missed-Call Burst Key Deduplication
- **File**: `scripts/webhook_server.py`
- **Details**:
  - Implement `build_missed_call_burst_key(data, resolved_context)` returning `missed-call:burst:<caller>:<line>` when both `from_number` and `to_number` are valid normalized numbers.
  - Update `claim_missed_call_notification(dedupe_key, *, burst_key=None, db_path=None, now_ms=None)` to atomically check and claim both the primary key and the burst key.
  - If `burst_key` is present and active within `MISSED_CALL_BURST_WINDOW_MS = 60 * 1000`, return `{"claimed": False, "duplicate": True, "key": dedupe_key, "status": "burst_duplicate"}`.
  - In `handle_call_webhook`, pass `burst_key=build_missed_call_burst_key(data, resolved)` to `claim_missed_call_notification`.
- **Test Scenarios**:
  - Two distinct `call_id`s with identical caller and line within 10s: first claims, second returns `duplicate=True, status="burst_duplicate"`.
  - Two calls from same caller on different line numbers within 10s: both claim successfully.
  - Two calls from same caller on same line separated by 70s: both claim successfully.
  - Call with unknown caller: burst dedupe skipped, relies on primary key.

### [U2] OpenClaw Hook Proxy Attribution Headers
- **File**: `scripts/webhook_server.py`
- **Details**:
  - Add helper `_resolve_openclaw_client_ip()` that reads `os.environ.get("OPENCLAW_HOOKS_CLIENT_IP")`, with fallback to local outbound IP detection and `10.0.0.1` safe fallback.
  - In `send_to_openclaw_hooks`, include `"X-Forwarded-For": _resolve_openclaw_client_ip()` in `req.headers`.
- **Test Scenarios**:
  - Default dispatch includes `X-Forwarded-For` header.
  - When `OPENCLAW_HOOKS_CLIENT_IP` is configured in env, that exact value is passed in `X-Forwarded-For`.
  - Live socket test against stock OpenClaw gateway accepts request without HTTP 403 `proxy_attribution_required`.

### [U3] Dialpad History Fetch Timestamp Guard and Resolution Optimization
- **File**: `scripts/webhook_server.py`
- **Details**:
  - In `_fetch_recent_calls_around`, cap `started_before`: `min(int(time.time() * 1000), event_ts_ms + window_ms)`.
  - Ensure `started_after < started_before` to avoid negative or zero ranges.
  - In `handle_call_webhook`, check if `resolved.get("caller_resolution_path") != "unresolved" and resolved.get("line_resolution_path") != "unresolved"`. If already resolved, skip re-running `resolve_missed_call_context(data)` with real network fetcher.
- **Test Scenarios**:
  - `_fetch_recent_calls_around` with `event_ts_ms = now_ms` never formats `started_before` greater than current timestamp.
  - `handle_call_webhook` does not call `history_fetcher` when `from_number` and `to_number` are directly present in payload.
  - `handle_call_webhook` invokes backfill only when caller or line is unresolved.

### [U4] Comprehensive Test Suite Expansion
- **Files**: `tests/test_webhook_hooks.py`, `tests/test_webhook_server.py`
- **Details**:
  - Add `test_claim_missed_call_notification_burst_deduplication` testing rapid bursts across distinct call IDs.
  - Add `test_claim_missed_call_notification_burst_window_expiry` verifying calls after 60s are admitted.
  - Add `test_send_to_openclaw_hooks_includes_forwarded_for_header` verifying client attribution headers.
  - Add `test_fetch_recent_calls_around_caps_future_timestamp` verifying `started_before <= now_ms`.
  - Add `test_handle_call_webhook_skips_history_fetch_when_already_resolved`.

---

## Verification Contract

### Automated Tests
Run pytest in the skill workspace:
```bash
pytest tests/test_webhook_server.py tests/test_webhook_hooks.py -v
```

### Production Verification (on `theshop`)
- Run unit tests on `theshop`.
- Verify curl to OpenClaw stock gateway `/hooks/agent` with `X-Forwarded-For` returns 400 (validation error) instead of 403 (`proxy_attribution_required`).
- Inspect `journalctl --user -u dialpad-webhook.service` to verify zero 403 hook errors and zero 400 history lookup errors.

---

## Definition of Done

1. Multi-leg Dialpad calls within 60s emit exactly 1 Telegram alert card.
2. Hook deliveries to `http://127.0.0.1:18789/hooks/agent` no longer produce 403 `proxy_attribution_required`.
3. History lookups no longer trigger 400 Bad Request `Timestamp range cannot be in the future`.
4. All existing and new automated tests pass without regressions.
