# OpenClaw Integration Guide

This guide explains how to connect OpenClaw to this Dialpad repo's webhook server.

Use this document when:
- wiring OpenClaw to receive inbound Dialpad SMS and missed-call events
- implementing the OpenClaw `/hooks/agent` receiver
- configuring human-in-the-loop review before outbound replies
- validating the integration in staging before production rollout

## Scope

This repo owns the Dialpad-side emission behavior:
- webhook receipt
- local storage/classification
- OpenClaw hook request construction
- graceful degradation when OpenClaw is unavailable

This repo does not validate:
- downstream OpenClaw prompt quality
- autonomous enrichment quality
- session semantics or dedupe behavior inside OpenClaw
- whether OpenClaw should auto-send without approval

## Default Operating Model

Recommended OpenClaw policy:
- proactively enrich inbound events
- generate a summary and recommended next action
- draft a response
- require human approval before any outbound send

Recommended default:

```json
{
  "dialpadHooks": {
    "enabled": true,
    "proactiveEnrichment": true,
    "sendMode": "approval_required"
  }
}
```

Suggested `sendMode` values:
- `draft_only`
- `approval_required`
- `auto_send`

`approval_required` should be the default.

## Dialpad-Side Environment

Set these on the Dialpad webhook server:

```bash
export DIALPAD_API_KEY="your-dialpad-api-key"
export DIALPAD_WEBHOOK_SECRET="your-dialpad-webhook-secret"

export OPENCLAW_GATEWAY_URL="http://127.0.0.1:8080"
export OPENCLAW_HOOKS_TOKEN="your-openclaw-hooks-token"
export OPENCLAW_HOOKS_PATH="/hooks/agent"
export OPENCLAW_HOOKS_NAME="Dialpad SMS"
export OPENCLAW_HOOKS_CALL_NAME="Dialpad Missed Call"
export OPENCLAW_HOOKS_SMS_ENABLED="1"
export OPENCLAW_HOOKS_CALL_ENABLED="1"
```

Behavior:
- when `OPENCLAW_HOOKS_TOKEN` is configured, inbound SMS forwarding is enabled by default unless `OPENCLAW_HOOKS_SMS_ENABLED=0`
- when `OPENCLAW_HOOKS_TOKEN` is configured, inbound missed-call forwarding is enabled by default unless `OPENCLAW_HOOKS_CALL_ENABLED=0`
- voicemail remains Telegram-only in this repo

## Dialpad Webhook Endpoints

Relevant endpoints in `scripts/webhook_server.py`:

- `POST /webhook/dialpad`
  - stores SMS
  - optionally forwards eligible inbound SMS to OpenClaw
  - optionally sends Telegram SMS alerts
- `POST /webhook/dialpad-call`
  - detects inbound missed calls
  - optionally forwards missed calls to OpenClaw
  - optionally sends Telegram missed-call alerts
- `POST /webhook/dialpad-voicemail`
  - Telegram-only in this repo

## OpenClaw Receiver Contract

OpenClaw should expose:

- `POST /hooks/agent`

Expected auth:

- `Authorization: Bearer <token>`

The token should match `OPENCLAW_HOOKS_TOKEN`.

### Required Request Fields

- `message`
- `name`
- `sessionKey`
- `deliver`

### Optional Request Fields

- `channel`
- `to`
- `agentId`

### SMS Example

```json
{
  "message": "📩 Dialpad SMS\nFrom: Jane Doe (+14155550123)\nTo: Sales (415) 520-1316\nTime: 1760000000000\n\nMessage: Need a callback",
  "name": "Dialpad SMS",
  "sessionKey": "hook:dialpad:sms:conv-123",
  "deliver": true,
  "channel": "telegram",
  "to": "-5102073225",
  "agentId": "niemand-work"
}
```

### Missed Call Example

```json
{
  "message": "📞 Dialpad Missed Call\nFrom: Jane Doe (+14155550123)\nLine: Sales (415) 520-1316\nTime: 1760000000000\nCall ID: call-123",
  "name": "Dialpad Missed Call",
  "sessionKey": "hook:dialpad:call:call-123",
  "deliver": true
}
```

### Response Contract

Recommended OpenClaw behavior:
- `200` for accepted payloads
- `401` for missing/invalid bearer token
- `400` for malformed JSON or missing required fields

Minimal success response:

```json
{"ok": true}
```

## Event Classification in OpenClaw

OpenClaw should treat the incoming event as:

- SMS when:
  - `name == "Dialpad SMS"`, or
  - `sessionKey` starts with `hook:dialpad:sms:`
- missed call when:
  - `name == "Dialpad Missed Call"`, or
  - `sessionKey` starts with `hook:dialpad:call:`

Prefer `sessionKey` as the stronger signal if there is ever a mismatch.

## Suggested Internal Event Model

Normalize the raw payload into something explicit before prompting the agent.

SMS:

```json
{
  "source": "dialpad",
  "eventType": "sms",
  "sessionKey": "hook:dialpad:sms:conv-123",
  "senderNumber": "+14155550123",
  "line": "Sales (415) 520-1316",
  "timestamp": 1760000000000,
  "body": "Need a callback",
  "callId": null
}
```

Missed call:

```json
{
  "source": "dialpad",
  "eventType": "missed_call",
  "sessionKey": "hook:dialpad:call:call-123",
  "senderNumber": "+14155550123",
  "line": "Sales (415) 520-1316",
  "timestamp": 1760000000000,
  "body": null,
  "callId": "call-123"
}
```

## OpenClaw Processing Flow

Recommended receiver flow:

1. authenticate request
2. validate required fields
3. persist raw event
4. normalize into internal event object
5. dedupe by `sessionKey`
6. trigger proactive enrichment
7. create a human review item
8. allow approve/edit/send if policy permits

## Human-in-the-Loop Workflow

The safe default is:

1. Dialpad event arrives
2. OpenClaw enriches contact/company context
3. OpenClaw produces:
   - summary
   - recommended action
   - draft reply or callback guidance
4. OpenClaw creates a reviewable inbox/task item
5. Human decides:
   - approve and send
   - edit then send
   - reject
   - mark for callback

Suggested state fields:

```json
{
  "sendMode": "approval_required",
  "requiresApproval": true,
  "approvalStatus": "pending",
  "deliveryStatus": "not_sent"
}
```

## Suggested Agent Output Shape

```json
{
  "summary": "Inbound SMS from Jane Doe asking for a callback about pricing.",
  "context": {
    "contactName": "Jane Doe",
    "companyName": "Acme",
    "relationship": "Lead"
  },
  "recommendedAction": "Reply by SMS within 15 minutes and offer two callback windows.",
  "draftReply": "Thanks, Jane. I can call you this afternoon at 2:00 PM or 3:30 PM. Which works better?",
  "priority": "high",
  "requiresApproval": true
}
```

For missed calls, `draftReply` may be:
- callback notes
- a suggested follow-up SMS
- a next-step recommendation instead of an immediate send

## Dedupe and Safety

OpenClaw should:
- persist every raw event for debugging
- dedupe using `sessionKey`
- never assume duplicate delivery means duplicate user intent
- separate "draft generation" from "send authority"

Outbound send must always enforce `sendMode`.

## Rollout Plan

Recommended rollout:

1. build a passive OpenClaw `/hooks/agent` receiver that authenticates, stores, and logs
2. point staging Dialpad webhook traffic at it
3. verify real SMS and missed-call payloads
4. add normalization and proactive enrichment
5. ship `approval_required` mode first
6. only enable `auto_send` if explicitly desired later

## Validation Checklist

Dialpad-side:
- `OPENCLAW_HOOKS_TOKEN` configured
- `OPENCLAW_HOOKS_PATH` points at a live OpenClaw receiver
- `DIALPAD_WEBHOOK_SECRET` matches the sender expectations
- test SMS and missed-call webhooks return HTTP 200

OpenClaw-side:
- `/hooks/agent` authenticates bearer token
- payloads are logged with secret redaction
- `sessionKey` is persisted for dedupe
- review items are created for inbound SMS and missed calls
- outbound replies require approval by default

## Monitoring

Useful log/search terms from the Dialpad side:
- `OpenClaw Hook`
- `Unauthorized webhook request on /webhook/dialpad`
- `Unauthorized webhook request on /webhook/dialpad-call`
- `request_failed`
- `token_missing`
- `disabled_by_config`

Healthy signals:
- inbound SMS and missed calls return HTTP 200
- expected `hook_status` values appear in logs
- Telegram missed-call behavior remains intact
- OpenClaw review items appear for both event types

Rollback lever:
- set `OPENCLAW_HOOKS_CALL_ENABLED=0` to stop missed-call forwarding
- set `OPENCLAW_HOOKS_SMS_ENABLED=0` to stop SMS forwarding

## Notes for Agents

If you are an agent setting this up:
- use this file as the primary setup guide
- do not assume autonomous sending is allowed
- implement `approval_required` as the default unless the operator explicitly chooses otherwise
