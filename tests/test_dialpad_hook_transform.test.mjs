import assert from "node:assert/strict";
import transform, { deriveDialpadSessionKey } from "../hooks/transforms/dialpad-hook-transform.mjs";

console.log("Running Dialpad Hook Transform Tests...");

// 1. SMS with conversationId
{
  const payload = {
    routing: {
      eventType: "sms",
      conversationId: "conv-123",
      senderNumber: "+14155550123",
      recipientNumber: "+14155559876",
    },
  };
  assert.equal(deriveDialpadSessionKey(payload), "hook:dialpad:sms:conv-123");
}

// 2. SMS without conversationId but with sender and recipient
{
  const payload = {
    routing: {
      eventType: "sms",
      senderNumber: "+1 (415) 555-0123",
      recipientNumber: "+1-415-555-9876",
    },
  };
  assert.equal(deriveDialpadSessionKey(payload), "hook:dialpad:sms:4155550123:4155559876");
}

// 3. SMS with messageId fallback
{
  const payload = {
    routing: {
      eventType: "sms",
      messageId: "msg-789",
    },
  };
  assert.equal(deriveDialpadSessionKey(payload), "hook:dialpad:sms:msg-789");
}

// 4. SMS with sender fallback
{
  const payload = {
    routing: {
      eventType: "sms",
      senderNumber: "+14155550123",
    },
  };
  assert.equal(deriveDialpadSessionKey(payload), "hook:dialpad:sms:4155550123");
}

// 5. SMS unknown fallback
{
  const payload = {
    routing: {
      eventType: "sms",
    },
  };
  assert.equal(deriveDialpadSessionKey(payload), "hook:dialpad:sms:unknown");
}

// 6. Missed call with callId
{
  const payload = {
    routing: {
      eventType: "missed_call",
      callId: "call-123",
    },
  };
  assert.equal(deriveDialpadSessionKey(payload), "hook:dialpad:call:call-123");
}

// 7. Missed call with sender + timestamp
{
  const payload = {
    routing: {
      eventType: "missed_call",
      senderNumber: "+1 (415) 555-0123",
      timestamp: 1760000000000,
    },
  };
  assert.equal(deriveDialpadSessionKey(payload), "hook:dialpad:call:4155550123:1760000000000");
}

// 8. Missed call with timestamp only
{
  const payload = {
    routing: {
      eventType: "missed_call",
      timestamp: 1760000000000,
    },
  };
  assert.equal(deriveDialpadSessionKey(payload), "hook:dialpad:call:1760000000000");
}

// 9. Missed call with sender only
{
  const payload = {
    routing: {
      eventType: "missed_call",
      senderNumber: "+14155550123",
    },
  };
  assert.equal(deriveDialpadSessionKey(payload), "hook:dialpad:call:4155550123");
}

// 10. Missed call unknown fallback
{
  const payload = {
    routing: {
      eventType: "missed_call",
    },
  };
  assert.equal(deriveDialpadSessionKey(payload), "hook:dialpad:call:unknown");
}

// 11. Full transform output
{
  const ctx = {
    payload: {
      message: "Hello world",
      name: "Dialpad SMS",
      deliver: true,
      channel: "telegram",
      to: "-12345",
      agentId: "niemand-work",
      routing: {
        eventType: "sms",
        conversationId: "conv-abc",
      },
    },
  };
  const action = transform(ctx);
  assert.equal(action.kind, "agent");
  assert.equal(action.sessionKey, "hook:dialpad:sms:conv-abc");
  assert.equal(action.sessionKeySource, "static");
  assert.equal(action.deliver, true);
  assert.equal(action.channel, "telegram");
  assert.equal(action.to, "-12345");
  assert.equal(action.agentId, "niemand-work");
  assert.equal(action.name, "Dialpad SMS");
  assert.equal(action.message, "Hello world");
}

// 12. Explicit derivedSessionKey passthrough
{
  const payload = {
    routing: {
      derivedSessionKey: "hook:dialpad:custom:key",
    },
  };
  assert.equal(deriveDialpadSessionKey(payload), "hook:dialpad:custom:key");
}

console.log("All Dialpad Hook Transform Tests passed successfully!");
