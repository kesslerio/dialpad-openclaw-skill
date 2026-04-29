import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import webhook_server
from webhook_server import (
    build_inbound_context,
    build_hook_session_key,
    build_openclaw_hook_payload,
    format_hook_message,
    normalize_call_hook_payload,
    normalize_sms_payload,
    parse_signature_candidates,
    parse_bool_env,
    verify_bearer_jwt,
    verify_hmac_signature,
    verify_webhook_auth,
)


def _b64url(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_hs256_jwt(secret, payload):
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url(sig)}"


def test_parse_signature_candidates_supports_raw_and_prefixed():
    digest = "a" * 64
    values = parse_signature_candidates(f"sha256={digest}, {digest}, v1:{digest}")
    assert values == [digest, digest, digest]


def test_parse_bool_env_supports_public_flag_values():
    assert parse_bool_env("1", False) is True
    assert parse_bool_env("true", False) is True
    assert parse_bool_env("0", True) is False
    assert parse_bool_env("off", True) is False
    assert parse_bool_env("", True) is True
    assert parse_bool_env(None, False) is False


def test_verify_hmac_signature_accepts_prefixed_header():
    secret = "supersecret"
    body = b'{"direction":"inbound","text":"hello"}'
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers = {"X-Dialpad-Signature": f"sha256={digest}"}
    assert verify_hmac_signature(body, headers, secret) is True


def test_verify_hmac_signature_rejects_missing_header_when_secret_required():
    secret = "supersecret"
    body = b"{}"
    assert verify_hmac_signature(body, {}, secret) is False


def test_verify_bearer_jwt_hs256():
    secret = "jwtsecret"
    token = _make_hs256_jwt(secret, {"sub": "dialpad-webhook"})
    headers = {"Authorization": f"Bearer {token}"}
    assert verify_bearer_jwt(headers, secret) is True
    assert verify_bearer_jwt(headers, "wrong-secret") is False


def test_verify_webhook_auth_accepts_hmac_or_jwt():
    secret = "combo-secret"
    body = b'{"event":"sms_received"}'
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    ok_hmac, source_hmac = verify_webhook_auth(
        {"X-Dialpad-Signature-SHA256": digest}, body, secret
    )
    assert ok_hmac is True
    assert source_hmac == "hmac"

    token = _make_hs256_jwt(secret, {"scope": "dialpad"})
    ok_jwt, source_jwt = verify_webhook_auth(
        {"Authorization": f"Bearer {token}"}, body, secret
    )
    assert ok_jwt is True
    assert source_jwt == "jwt"


def test_build_hook_session_key_fallback_order():
    assert build_hook_session_key({"conversation_id": "conv-1", "message_id": "msg-1"}) == "hook:dialpad:sms:conv-1"
    assert build_hook_session_key({"conversation_id": None, "message_id": "msg-1"}) == "hook:dialpad:sms:msg-1"
    assert build_hook_session_key({"sender_number": "+1 (415) 555-0123"}) == "hook:dialpad:sms:4155550123"
    assert build_hook_session_key({}) == "hook:dialpad:sms:unknown"


def test_normalize_and_format_hook_message():
    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155559876"],
        "text_content": "Need a callback",
        "event_timestamp": 1760000000000,
        "id": "m-123",
    }
    normalized = normalize_sms_payload(payload, contact_info="Jane Doe")

    assert normalized["sender"] == "Jane Doe"
    assert normalized["sender_number"] == "+14155550123"
    assert normalized["recipient_number"] == "+14155559876"
    assert normalized["text"] == "Need a callback"
    assert normalized["timestamp"] == 1760000000000
    assert normalized["message_id"] == "m-123"
    assert normalized["direction"] == "inbound"

    message = format_hook_message(normalized, line_display="Support (415) 555-9876")
    assert "📩 Dialpad SMS" in message
    assert "To: Support (415) 555-9876" in message
    assert "From: Jane Doe (+14155550123)" in message
    assert "Message: Need a callback" in message


def test_hook_payload_includes_optional_agent_channel_and_to(monkeypatch):
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_NAME", "Dialpad SMS")
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_CHANNEL", "telegram")
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_TO", "-5102073225")
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_AGENT_ID", "niemand-work")

    normalized = {
        "sender": "Jane Doe",
        "sender_number": "+14155550123",
        "recipient_number": "+14155559876",
        "text": "Ping",
        "conversation_id": "conv-123",
        "first_contact": {
            "knownContact": False,
            "needsIdentityLookup": True,
            "needsBusinessContext": True,
            "needsDraftReply": True,
            "needsDialpadContactSync": True,
            "keepBrief": False,
            "identityState": "not_found",
            "contactName": None,
            "senderNumber": "+14155550123",
            "recipientNumber": "+14155559876",
            "lineDisplay": "Support",
            "eventType": "sms",
            "lookup": {
                "status": "not_found",
                "degraded": False,
                "degradedReason": None,
            },
        },
        "auto_reply": {
            "eligible": True,
            "sent": True,
            "status": "accepted/queued",
            "message": "Hi there, thanks for reaching ShapeScale for Business Sales. We got your message and will be in touch shortly.",
        },
    }

    payload = build_openclaw_hook_payload(normalized, line_display="Support")
    assert payload["name"] == "Dialpad SMS"
    assert payload["channel"] == "telegram"
    assert payload["to"] == "-5102073225"
    assert payload["agentId"] == "niemand-work"
    assert payload["sessionKey"] == "hook:dialpad:sms:conv-123"
    assert payload["firstContact"]["needsDraftReply"] is True
    assert payload["firstContact"]["identityState"] == "not_found"
    assert payload["autoReply"]["sent"] is True


def test_build_hook_session_key_for_missed_call_fallback_order():
    assert build_hook_session_key({"event_type": "missed_call", "call_id": "call-1"}) == "hook:dialpad:call:call-1"
    assert build_hook_session_key(
        {"event_type": "missed_call", "call_id": None, "sender_number": "+1 (415) 555-0123", "timestamp": 1760000000000}
    ) == "hook:dialpad:call:4155550123:1760000000000"
    assert build_hook_session_key({"event_type": "missed_call", "timestamp": 1760000000000}) == "hook:dialpad:call:1760000000000"
    assert build_hook_session_key({"event_type": "missed_call"}) == "hook:dialpad:call:unknown"


def test_normalize_call_payload_and_format_hook_message():
    payload = {
        "direction": "inbound",
        "call_direction": "inbound",
        "call_id": "call-123",
        "date_started": 1760000000000,
    }
    resolved = {
        "from_number": "+14155550123",
        "to_number": "+14155559876",
        "line_display": "Support (415) 555-9876",
        "event_ts_ms": 1760000000000,
    }

    normalized = normalize_call_hook_payload(payload, resolved, contact_info="Jane Doe")

    assert normalized["event_type"] == "missed_call"
    assert normalized["sender"] == "Jane Doe"
    assert normalized["sender_number"] == "+14155550123"
    assert normalized["recipient_number"] == "+14155559876"
    assert normalized["timestamp"] == 1760000000000
    assert normalized["call_id"] == "call-123"

    message = format_hook_message(normalized, line_display=normalized["line_display"])
    assert "📞 Dialpad Missed Call" in message
    assert "Line: Support (415) 555-9876" in message
    assert "From: Jane Doe (+14155550123)" in message
    assert "Call ID: call-123" in message


def test_call_hook_payload_uses_shared_envelope(monkeypatch):
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_NAME", "Dialpad Inbox")
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_CALL_NAME", "Dialpad Missed Call")

    normalized = {
        "event_type": "missed_call",
        "sender": "Jane Doe",
        "sender_number": "+14155550123",
        "recipient_number": "+14155559876",
        "line_display": "Support",
        "timestamp": 1760000000000,
        "call_id": "call-123",
        "first_contact": {
            "identityState": "resolved",
            "knownContact": True,
            "needsIdentityLookup": False,
            "needsBusinessContext": False,
            "needsDraftReply": False,
            "needsDialpadContactSync": False,
            "keepBrief": True,
            "contactName": "Jane Doe",
            "senderNumber": "+14155550123",
            "recipientNumber": "+14155559876",
            "lineDisplay": "Support",
            "eventType": "missed_call",
            "lookup": {
                "status": "resolved",
                "degraded": False,
                "degradedReason": None,
            },
        },
        "auto_reply": {
            "eligible": False,
            "sent": False,
            "status": "not_eligible",
            "message": None,
        },
    }

    payload = build_openclaw_hook_payload(normalized, line_display="Support")
    assert payload["name"] == "Dialpad Missed Call"
    assert payload["deliver"] is True
    assert payload["sessionKey"] == "hook:dialpad:call:call-123"
    assert "📞 Dialpad Missed Call" in payload["message"]
    assert payload["firstContact"]["identityState"] == "resolved"
    assert payload["firstContact"]["keepBrief"] is True
    assert payload["autoReply"]["eligible"] is False


def test_hook_payload_includes_inbound_context_for_known_recent_contact():
    normalized = {
        "event_type": "missed_call",
        "sender": "Ann Harper",
        "sender_number": "+14322083277",
        "recipient_number": "+14155201316",
        "line_display": "Sales",
        "timestamp": 1760000000000,
        "call_id": "call-123",
    }
    first_contact = webhook_server.build_first_contact_context(
        normalized,
        sender_enrichment={
            "contact_name": "Ann Harper",
            "status": "resolved",
            "degraded": False,
            "degraded_reason": None,
        },
        line_display="Sales",
    )
    normalized["first_contact"] = first_contact
    normalized["inbound_context"] = build_inbound_context(
        normalized,
        sender_enrichment={
            "contact_name": "Ann Harper",
            "status": "resolved",
            "degraded": False,
            "degraded_reason": None,
        },
        line_display="Sales",
        recent_context={
            "source": "dialpad_call_history",
            "lastActivityAt": 1759913600000,
        },
    )

    payload = build_openclaw_hook_payload(normalized, line_display="Sales")

    assert payload["firstContact"]["knownContact"] is True
    assert payload["inboundContext"]["identityConfidence"] == "high"
    assert payload["inboundContext"]["contextDraftAllowed"] is True
    assert "exact_phone_match" in payload["inboundContext"]["evidence"]
    assert "dialpad_call_history" in payload["inboundContext"]["evidence"]


def test_inbound_context_blocks_stale_known_contact_draft():
    normalized = {
        "event_type": "sms",
        "sender": "Ann Harper",
        "sender_number": "+14322083277",
        "recipient_number": "+14155201316",
        "timestamp": 1760000000000,
    }
    sender_enrichment = {
        "contact_name": "Ann Harper",
        "status": "resolved",
        "degraded": False,
        "degraded_reason": None,
    }
    normalized["first_contact"] = webhook_server.build_first_contact_context(
        normalized,
        sender_enrichment=sender_enrichment,
        line_display="Sales",
    )

    context = build_inbound_context(
        normalized,
        sender_enrichment=sender_enrichment,
        line_display="Sales",
        recent_context={
            "source": "local_sms_history",
            "lastActivityAt": 1758617600000,
        },
    )

    assert context["knownContact"] is True
    assert context["identityConfidence"] == "high"
    assert context["recency"]["state"] == "stale"
    assert context["contextDraftAllowed"] is False


def test_payload_contact_name_is_not_resolved_identity():
    sender_enrichment = webhook_server.apply_payload_contact_fallback(
        {
            "contact_name": None,
            "status": "not_found",
            "degraded": False,
            "degraded_reason": None,
        },
        {"contact": {"name": "Payload Person"}},
    )
    normalized = {
        "event_type": "missed_call",
        "sender_number": "+14322083277",
        "recipient_number": "+14155201316",
        "timestamp": 1760000000000,
    }
    normalized["first_contact"] = webhook_server.build_first_contact_context(
        normalized,
        sender_enrichment=sender_enrichment,
        line_display="Sales",
    )
    context = build_inbound_context(
        normalized,
        sender_enrichment=sender_enrichment,
        line_display="Sales",
        recent_context={
            "source": "dialpad_call_history",
            "lastActivityAt": 1759913600000,
        },
    )

    assert sender_enrichment["status"] == "payload_contact"
    assert normalized["first_contact"]["identityState"] == "payload_contact"
    assert normalized["first_contact"]["knownContact"] is False
    assert context["identityConfidence"] == "low"
    assert context["contextDraftAllowed"] is False
    assert "exact_phone_match" not in context["evidence"]
    assert "webhook_contact_payload" in context["evidence"]


def test_recent_sms_context_does_not_self_match_without_event_identity(monkeypatch):
    def _fail_if_called():
        raise AssertionError("history DB should not be opened without event id or timestamp")

    monkeypatch.setattr(webhook_server, "init_sms_history_db", _fail_if_called)

    assert webhook_server.lookup_recent_sms_context("+14322083277") is None
