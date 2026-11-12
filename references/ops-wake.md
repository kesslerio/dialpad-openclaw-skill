# Ops assistant wake

`scripts/ops_wake.py` lets the webhook server wake an external ops assistant
the moment inbound Dialpad activity arrives, so the assistant does not need to
poll Dialpad on a timer. It reuses the existing call and SMS subscriptions; no
new Dialpad subscription is required.

## What wakes

| Dialpad event (existing subscription) | Wake `event` |
|---|---|
| SMS, inbound, with text or media | `inbound_sms` |
| Call `missed` | `missed_call` (or `voicemail` when a voicemail is attached) |
| Call `hangup`, inbound, connected | `inbound_call` |
| Call `hangup`, inbound, voicemail attached | `voicemail` |
| Call `hangup`, inbound, never connected | `missed_call` |

Outbound, internal, and delivery-status events never wake. Missed-call hints
routed through the SMS path are left to the call subscription.

## Payload

```json
{
  "source": "dialpad",
  "event": "missed_call",
  "line": "+1XXXXXXXXXX",
  "occurred_at": "2026-01-01T17:00:00Z",
  "dialpad": {"call_id": "...", "entry_point_call_id": "...", "master_call_id": "...", "state": "missed", "voicemail": false},
  "sent_at": "2026-01-01T17:00:01Z",
  "suppressed_since_last": 0
}
```

SMS wakes carry `dialpad.message_id`, `conversation_id`, and `mms` instead.
Message bodies, caller numbers, transcripts, and recording links are never
sent; the receiver re-reads the event from Dialpad.

## Configuration

Put these in a mode-600 env file loaded by the webhook service (for example a
dedicated `EnvironmentFile=` drop-in). The forward is off unless both URL and
key are set.

```
OPS_WAKE_URL=https://...           # https only (http allowed for loopback)
OPS_WAKE_KEY=...                   # sent as Authorization: Bearer
OPS_WAKE_LINES=+1...,+1...         # optional; empty = every line
OPS_WAKE_MAX_PER_MINUTE=6          # optional
OPS_WAKE_MAX_PER_HOUR=40           # optional
OPS_WAKE_DEDUPE_DB=/home/art/niemand/logs/ops_wake.db   # optional
```

## Guarantees

- The dedupe claim, rate check, and POST all run on a daemon thread; a slow,
  locked, or failing store or wake URL never delays or fails the webhook
  response.
- One wake per Dialpad id and event kind (SQLite claim, 7-day retention), so
  Dialpad retries and repeated call-state events do not re-wake. If the claim
  store is unavailable the wake is skipped (fails closed).
- Bursts past the per-minute or per-hour cap are dropped and counted; the next
  wake reports the count in `suppressed_since_last` so the receiver can sweep.
- Single attempt, no retry queue. A lost wake is recovered by the receiver's
  next sweep.
