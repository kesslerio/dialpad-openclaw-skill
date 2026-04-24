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
- autonomous outbound sending; SMS send authority stays in the deterministic approval ledger

## Default Operating Model

Recommended OpenClaw policy:
- proactively enrich inbound events
- generate a summary and recommended next action
- draft a response
- require deterministic human approval before any outbound send

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

`approval_required` should be the default. `auto_send` is intentionally unsupported for inbound Dialpad-triggered SMS.

## Dialpad-Side Environment

Set these on the Dialpad webhook server:

```bash
export DIALPAD_API_KEY="your-dialpad-api-key"
export DIALPAD_WEBHOOK_SECRET="your-dialpad-webhook-secret"

export OPENCLAW_GATEWAY_URL="http://127.0.0.1:18789"
export OPENCLAW_HOOKS_TOKEN="your-openclaw-hooks-token"
export OPENCLAW_HOOKS_PATH="/hooks/agent"
export OPENCLAW_HOOKS_NAME="Dialpad SMS"
export OPENCLAW_HOOKS_CALL_NAME="Dialpad Missed Call"
export OPENCLAW_HOOKS_AGENT_ID="niemand-work"
export OPENCLAW_HOOKS_SMS_ENABLED="0"
export OPENCLAW_HOOKS_CALL_ENABLED="0"
export DIALPAD_AUTO_REPLY_ENABLED="0"
export DIALPAD_SMS_APPROVAL_DB="/home/art/clawd/logs/sms_approvals.db"
export DIALPAD_SMS_APPROVAL_TOKEN="operator-only-random-token"
```

Behavior:
- when `OPENCLAW_HOOKS_TOKEN` is configured, inbound SMS forwarding still requires `OPENCLAW_HOOKS_SMS_ENABLED=1`
- when `OPENCLAW_HOOKS_TOKEN` is configured, inbound missed-call forwarding still requires `OPENCLAW_HOOKS_CALL_ENABLED=1`
- when `DIALPAD_AUTO_REPLY_ENABLED` is truthy, first-contact messages on the sales line `(415) 520-1316` create exact-text approval drafts instead of sending SMS directly
- voicemail remains Telegram-only for OpenClaw fan-out, but first-contact sales-line voicemails can create SMS approval drafts when draft creation is enabled
- explicit opt-out language creates no draft, invalidates pending drafts for that customer, and emits only a human-only Telegram notice
- CLI approval is disabled unless `DIALPAD_SMS_APPROVAL_TOKEN` is configured and supplied by the operator approval surface
- if your gateway listens on a different port, change `OPENCLAW_GATEWAY_URL` accordingly
- the local gateway allows explicit `niemand-work` routing and the `hook:dialpad:` session-key namespace

## Dialpad Webhook Endpoints

Relevant endpoints in `scripts/webhook_server.py`:

- `POST /webhook/dialpad`
  - stores SMS
  - optionally forwards eligible inbound SMS to OpenClaw
  - optionally sends Telegram SMS alerts
  - creates approval drafts for eligible first-contact sales-line replies, but does not send SMS directly
- `POST /webhook/dialpad-call`
  - detects inbound missed calls
  - optionally forwards missed calls to OpenClaw
  - optionally sends Telegram missed-call alerts using the event timestamp when available
  - escapes dynamic Telegram fields before sending
  - creates approval drafts for eligible first-contact sales-line missed-call acknowledgments, but does not send SMS directly
- `POST /webhook/dialpad-voicemail`
  - Telegram notification plus optional approval-draft creation for eligible first-contact sales-line voicemails

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
- `firstContact`
- `autoReply`

### First-Contact Assist Hint

The webhook may include a `firstContact` object for first-time or otherwise unknown inbound contacts. It is an additive hint, not a required field.

```json
{
  "identityState": "not_found",
  "knownContact": false,
  "needsIdentityLookup": true,
  "needsBusinessContext": true,
  "needsDraftReply": true,
  "needsDialpadContactSync": true,
  "keepBrief": false,
  "contactName": null,
  "senderNumber": "+14155550123",
  "recipientNumber": "+14155201316",
  "lineDisplay": "Sales (415) 520-1316",
  "eventType": "sms",
  "lookup": {
    "status": "not_found",
    "degraded": false,
    "degradedReason": null
  }
}
```

Interpretation:

- `identityState` is the normalized identity result carried through the hook; only `resolved` is safe to treat as auto-mutable
- when `knownContact` is `false`, do the identity/business lookup first
- use Attio if that is your source of truth, or plug in a different CRM/directory if you do not use Attio
- if lookup is still ambiguous, use web research as fallback and keep the output concise
- if the match is clear, suggest Dialpad contact normalization or update
- if the evidence is only first name, area code, industry, or job title, keep the lead ambiguous and draft-only until stronger proof appears
- if `keepBrief` is `true`, skip the long background pass and stay short
- if `autoReply` is present, treat it as approval-draft metadata. `sent: false` means the webhook created or attempted a draft and downstream automation must not send the same reply directly.
- if `autoReply.replyPolicy.state` is `risky`, the approval path must require a second confirmation.
- if opt-out language is detected, the event is not forwarded as a normal hook payload; automation must remain human-only.

### SMS Example

```json
{
  "message": "📩 Dialpad SMS\nFrom: Jane Doe (+14155550123)\nTo: Sales (415) 520-1316\nTime: 1760000000000\n\nMessage: Need a callback",
  "name": "Dialpad SMS",
  "sessionKey": "hook:dialpad:sms:conv-123",
  "deliver": true,
  "channel": "telegram",
  "to": "-5102073225",
  "agentId": "niemand-work",
  "firstContact": {
    "knownContact": false,
    "needsIdentityLookup": true,
    "needsBusinessContext": true,
    "needsDraftReply": true,
    "needsDialpadContactSync": true,
    "keepBrief": false,
    "contactName": null,
    "senderNumber": "+14155550123",
    "recipientNumber": "+14155201316",
    "lineDisplay": "Sales (415) 520-1316",
    "eventType": "sms",
    "lookup": {
      "status": "not_found",
      "degraded": false,
      "degradedReason": null
    }
  },
  "autoReply": {
    "eligible": true,
    "sent": false,
    "draftCreated": true,
    "draftId": "smsdraft_abc123",
    "status": "draft_created",
    "replyPolicy": {
      "state": "normal",
      "reason_code": "eligible",
      "risk_reason": null
    },
    "message": "Hi there, thanks for reaching ShapeScale for Business Sales. We got your message and will be in touch shortly."
  }
}
```

### Missed Call Example

```json
{
  "message": "📞 Dialpad Missed Call\nFrom: Jane Doe (+14155550123)\nLine: Sales (415) 520-1316\nTime: 1760000000000\nCall ID: call-123",
  "name": "Dialpad Missed Call",
  "sessionKey": "hook:dialpad:call:call-123",
  "deliver": true,
  "firstContact": {
    "knownContact": false,
    "needsIdentityLookup": true,
    "needsBusinessContext": true,
    "needsDraftReply": true,
    "needsDialpadContactSync": true,
    "keepBrief": false,
    "contactName": null,
    "senderNumber": "+14155550123",
    "recipientNumber": "+14155201316",
    "lineDisplay": "Sales (415) 520-1316",
    "eventType": "missed_call",
    "lookup": {
      "status": "not_found",
      "degraded": false,
      "degradedReason": null
    }
  },
  "autoReply": {
    "eligible": true,
    "sent": false,
    "draftCreated": true,
    "draftId": "smsdraft_def456",
    "status": "draft_created",
    "replyPolicy": {
      "state": "normal",
      "reason_code": "eligible",
      "risk_reason": null
    },
    "message": "Hi there, you've reached ShapeScale for Business Sales. Sorry we missed your call. How can we help?"
  }
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
  "callId": null,
  "firstContact": {
    "identityState": "not_found",
    "knownContact": false,
    "needsIdentityLookup": true,
    "needsBusinessContext": true,
    "needsDraftReply": true,
    "needsDialpadContactSync": true,
    "keepBrief": false,
    "contactName": null,
    "senderNumber": "+14155550123",
    "recipientNumber": "+14155201316",
    "lineDisplay": "Sales (415) 520-1316",
    "eventType": "sms",
    "lookup": {
      "status": "not_found",
      "degraded": false,
      "degradedReason": null
    }
  },
  "autoReply": {
    "eligible": true,
    "sent": false,
    "draftCreated": true,
    "draftId": "smsdraft_abc123",
    "status": "draft_created",
    "replyPolicy": {
      "state": "normal",
      "reason_code": "eligible",
      "risk_reason": null
    },
    "message": "Hi there, thanks for reaching ShapeScale for Business Sales. We got your message and will be in touch shortly."
  }
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
  "callId": "call-123",
  "firstContact": {
    "identityState": "resolved",
    "knownContact": true,
    "needsIdentityLookup": false,
    "needsBusinessContext": false,
    "needsDraftReply": false,
    "needsDialpadContactSync": false,
    "keepBrief": true,
    "contactName": "Jane Doe",
    "senderNumber": "+14155550123",
    "recipientNumber": "+14155201316",
    "lineDisplay": "Sales (415) 520-1316",
    "eventType": "missed_call",
    "lookup": {
      "status": "resolved",
      "degraded": false,
      "degradedReason": null
    }
  }
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

Current-turn verification applies here too:

- "Already sent" and "Already updated" are only valid after a fresh tool result in the same turn.
- Stale session memory is not proof of a send or contact update.
- If the current turn has not verified the action yet, say so plainly and continue with the send/update step.

Suggested identity states:

- `resolved` when the payload already carries a strong contact match or the downstream CRM proves identity with strong evidence
- `ambiguous` when the downstream CRM can narrow to a candidate but not prove it, or the evidence is too soft to mutate
- `not_found` when no strong match exists yet and the lead should stay draft-only until more evidence appears
- `degraded` when lookup failed or is partially unavailable

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
- require `identityState == "resolved"` before any contact mutation; `ambiguous`, `not_found`, and `degraded` stay draft-only
- require current-turn verification before any success claim about sending or updating
- treat stale context as non-evidence for "Already sent" or "Already updated"

Outbound send must always go through `scripts/approve_sms_draft.py` or an equivalent deterministic approval handler with a real human actor id plus a trusted approval token/callback. The agent or bot must not approve its own draft.

## Rollout Plan

Recommended rollout:

1. build a passive OpenClaw `/hooks/agent` receiver that authenticates, stores, and logs
2. point staging Dialpad webhook traffic at it
3. verify real SMS and missed-call payloads
4. add normalization and proactive enrichment
5. ship `approval_required` mode first
6. re-enable hook classes only after approval drafts and stale/opt-out behavior are verified

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
- outbound replies require approval; autonomous SMS send is not supported for inbound Dialpad-triggered events

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
