import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webhook_server
from webhook_server import (
    build_hook_session_key,
    build_openclaw_hook_payload,
    format_hook_message,
    normalize_sms_payload,
    parse_signature_candidates,
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
    assert "Dialpad inbound SMS" in message
    assert "To Line: Support (415) 555-9876" in message
    assert "From: Jane Doe (+14155550123)" in message
    assert "Need a callback" in message


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
    }

    payload = build_openclaw_hook_payload(normalized, line_display="Support")
    assert payload["name"] == "Dialpad SMS"
    assert payload["channel"] == "telegram"
    assert payload["to"] == "-5102073225"
    assert payload["agentId"] == "niemand-work"
    assert payload["sessionKey"] == "hook:dialpad:sms:conv-123"
