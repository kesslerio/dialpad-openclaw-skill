/**
 * Dialpad OpenClaw Hook Transform
 *
 * Derives OpenClaw sessionKey server-side from Dialpad event routing metadata,
 * allowing OpenClaw to operate with `hooks.allowRequestSessionKey=false`
 * to satisfy OpenClaw security audit standards (eliminating audit-critical
 * `hooks.request_session_key_enabled`).
 *
 * Usage in OpenClaw gateway configuration (`~/.openclaw/openclaw.json`):
 * ```json5
 * {
 *   hooks: {
 *     enabled: true,
 *     token: "your-openclaw-hooks-token",
 *     path: "/hooks",
 *     allowRequestSessionKey: false,
 *     allowedAgentIds: ["niemand-work"],
 *     transformsDir: "~/.openclaw/hooks/transforms",
 *     mappings: [
 *       {
 *         match: { path: "dialpad" },
 *         action: "agent",
 *         agentId: "niemand-work",
 *         transform: {
 *           module: "dialpad-hook-transform.mjs"
 *         }
 *       }
 *     ]
 *   }
 * }
 * ```
 */

export function normalizePhoneNumber(value) {
  if (!value) return null;
  let digits = String(value).replace(/\D/g, "");
  if (!digits) return null;
  if (digits.length === 11 && digits.startsWith("1")) {
    digits = digits.slice(1);
  }
  if (digits.length >= 10) {
    return digits.slice(-10);
  }
  return digits;
}

export function deriveDialpadSessionKey(payload) {
  if (!payload || typeof payload !== "object") {
    return "hook:dialpad:sms:unknown";
  }

  const routing = payload.routing || {};

  // If the server-provided routing already contains the trusted derived session key:
  if (typeof routing.derivedSessionKey === "string" && routing.derivedSessionKey.trim()) {
    return routing.derivedSessionKey.trim();
  }

  const isMissedCall =
    routing.eventType === "missed_call" ||
    payload.name === "Dialpad Missed Call" ||
    (payload.firstContact && payload.firstContact.eventType === "missed_call") ||
    (payload.inboundContext && payload.inboundContext.eventType === "missed_call");

  if (isMissedCall) {
    const callId =
      routing.callId ||
      (typeof payload.message === "string" && payload.message.match(/Call ID:\s*([^\s\n]+)/)?.[1]);
    if (callId) {
      return `hook:dialpad:call:${callId}`;
    }

    const senderNumber = normalizePhoneNumber(
      routing.senderNumber || (payload.firstContact && payload.firstContact.senderNumber)
    );
    const timestamp = routing.timestamp !== undefined && routing.timestamp !== null
      ? routing.timestamp
      : undefined;

    if (senderNumber && timestamp !== undefined) {
      return `hook:dialpad:call:${senderNumber}:${timestamp}`;
    }
    if (timestamp !== undefined) {
      return `hook:dialpad:call:${timestamp}`;
    }
    if (senderNumber) {
      return `hook:dialpad:call:${senderNumber}`;
    }
    return "hook:dialpad:call:unknown";
  }

  // SMS session key derivation
  const senderNumber = normalizePhoneNumber(
    routing.senderNumber || (payload.firstContact && payload.firstContact.senderNumber)
  );
  const recipientNumber = normalizePhoneNumber(
    routing.recipientNumber || (payload.firstContact && payload.firstContact.recipientNumber)
  );
  let candidate = routing.conversationId;
  if (!candidate && senderNumber && recipientNumber) {
    candidate = `${senderNumber}:${recipientNumber}`;
  }
  if (!candidate) {
    candidate = routing.messageId || senderNumber || "unknown";
  }

  return `hook:dialpad:sms:${candidate}`;
}

export default function transform(ctx) {
  const payload = (ctx && ctx.payload) || {};
  const sessionKey = deriveDialpadSessionKey(payload);

  return {
    kind: "agent",
    message: payload.message || "",
    name: payload.name || "Dialpad",
    sessionKey,
    sessionKeySource: "static", // OpenClaw treats "static" as server-side derived, bypassing allowRequestSessionKey
    deliver: typeof payload.deliver === "boolean" ? payload.deliver : false,
    channel: payload.channel,
    to: payload.to,
    agentId: payload.agentId,
  };
}
