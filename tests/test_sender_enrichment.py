import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import webhook_server


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status = 200

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _build_handler(payload):
    raw = json.dumps(payload).encode("utf-8")
    handler = object.__new__(webhook_server.DialpadWebhookHandler)
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 12345)

    status = {"code": None}

    def _send_response(code):
        status["code"] = code

    def _send_header(_name, _value):
        return None

    def _end_headers():
        return None

    def _send_error(code, _message=None):
        status["code"] = code

    handler.send_response = _send_response
    handler.send_header = _send_header
    handler.end_headers = _end_headers
    handler.send_error = _send_error
    return handler, status


def test_lookup_contact_enrichment_valid_token_path(monkeypatch):
    payload = {
        "items": [
            {
                "first_name": "Jane",
                "last_name": "Doe",
                "company": "Acme",
                "job_title": "VP Sales",
            }
        ]
    }
    monkeypatch.setattr(webhook_server, "DIALPAD_API_KEY", "token-123")
    monkeypatch.setattr(
        webhook_server.urllib.request,
        "urlopen",
        lambda _req, timeout=5: _FakeResponse(payload),
    )

    result = webhook_server.lookup_contact_enrichment("+14155550123")
    assert result["contact_name"] == "VP Sales | Jane Doe (Acme)"
    assert result["status"] == "resolved"
    assert result["degraded"] is False
    assert result["degraded_reason"] is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"error":"token expired"}', "expired_token"),
        (b'{"error":{"message":"missing scope contacts:read"}}', "missing_scope"),
        (b'{"error":{"message":"invalid audience for production"}}', "invalid_audience_or_environment"),
        (b'{"error":"unauthorized"}', "unauthorized"),
    ],
)
def test_classify_contact_lookup_unauthorized(body, expected):
    assert webhook_server.classify_contact_lookup_unauthorized(body) == expected


def test_lookup_contact_enrichment_401_degraded_and_cached_fallback(monkeypatch):
    body = b'{"error":{"message":"Access token expired"}}'
    http_error = urllib.error.HTTPError(
        url="https://dialpad.com/api/v2/contacts?query=14155550123",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=io.BytesIO(body),
    )

    def _raise_401(_req, timeout=5):
        raise http_error

    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "DIALPAD_API_KEY", "token-123")
    monkeypatch.setattr(webhook_server.urllib.request, "urlopen", _raise_401)
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Cached Person"}},
    )
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", False)

    hook_calls = []

    def _fake_hook(normalized_sms, line_display=None):
        hook_calls.append({"normalized_sms": normalized_sms, "line_display": line_display})
        return True, "http_200"

    monkeypatch.setattr(webhook_server, "send_sms_to_openclaw_hooks", _fake_hook)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda _text: True)

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Need callback",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    response = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert status["code"] == 200
    assert response["sender_enrichment_degraded"] is True
    assert response["sender_enrichment_degraded_reason"] == "expired_token"
    assert hook_calls[0]["normalized_sms"]["sender"] == "Cached Person"
    assert hook_calls[0]["normalized_sms"]["first_contact"]["knownContact"] is True
    assert hook_calls[0]["normalized_sms"]["first_contact"]["keepBrief"] is True
    assert hook_calls[0]["normalized_sms"]["first_contact"]["identityState"] == "degraded"


def test_inbound_telegram_uses_enriched_sender(monkeypatch):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Unknown"}},
    )
    monkeypatch.setattr(
        webhook_server,
        "lookup_contact_enrichment",
        lambda _number: {
            "contact_name": "Jane Doe",
            "status": "resolved",
            "degraded": False,
            "degraded_reason": None,
        },
    )
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_sms_to_openclaw_hooks", lambda *_args, **_kwargs: (True, "http_200"))

    telegram_messages = []
    monkeypatch.setattr(
        webhook_server,
        "send_to_telegram",
        lambda text: telegram_messages.append(text) or True,
    )

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Inbound hello",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    assert status["code"] == 200
    assert len(telegram_messages) == 1
    assert "From: Jane Doe (+14155550123)" in telegram_messages[0]


def test_inbound_webhook_hook_uses_enriched_sender(monkeypatch):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Unknown"}},
    )
    monkeypatch.setattr(
        webhook_server,
        "lookup_contact_enrichment",
        lambda _number: {
            "contact_name": "Jane Doe",
            "status": "resolved",
            "degraded": False,
            "degraded_reason": None,
        },
    )
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", False)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda _text: True)

    hook_calls = []

    def _fake_hook(normalized_sms, line_display=None):
        hook_calls.append({"normalized_sms": normalized_sms, "line_display": line_display})
        return True, "http_200"

    monkeypatch.setattr(webhook_server, "send_sms_to_openclaw_hooks", _fake_hook)

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Inbound hello",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    response = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert status["code"] == 200
    assert hook_calls[0]["normalized_sms"]["sender"] == "Jane Doe"
    assert hook_calls[0]["normalized_sms"]["first_contact"]["knownContact"] is True
    assert hook_calls[0]["normalized_sms"]["first_contact"]["keepBrief"] is True
    assert hook_calls[0]["normalized_sms"]["first_contact"]["identityState"] == "resolved"
    assert response["hook_forwarded"] is True
    assert response["sender_enrichment_status"] == "resolved"
    assert response["sender_enrichment_degraded"] is False


def test_inbound_webhook_hook_marks_unknown_sender_first_contact_candidate(monkeypatch):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Unknown"}},
    )
    monkeypatch.setattr(
        webhook_server,
        "lookup_contact_enrichment",
        lambda _number: {
            "contact_name": None,
            "status": "not_found",
            "degraded": False,
            "degraded_reason": None,
        },
    )
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", False)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda _text: True)

    hook_calls = []

    def _fake_hook(normalized_sms, line_display=None):
        hook_calls.append({"normalized_sms": normalized_sms, "line_display": line_display})
        return True, "http_200"

    monkeypatch.setattr(webhook_server, "send_sms_to_openclaw_hooks", _fake_hook)

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Who is this?",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    assert status["code"] == 200
    assert hook_calls[0]["normalized_sms"]["first_contact"]["knownContact"] is False
    assert hook_calls[0]["normalized_sms"]["first_contact"]["needsIdentityLookup"] is True
    assert hook_calls[0]["normalized_sms"]["first_contact"]["needsDraftReply"] is True
    assert hook_calls[0]["normalized_sms"]["first_contact"]["keepBrief"] is False
    assert hook_calls[0]["normalized_sms"]["first_contact"]["identityState"] == "not_found"


def test_inbound_sales_sms_auto_replies_on_first_contact(monkeypatch):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Unknown"}},
    )
    monkeypatch.setattr(
        webhook_server,
        "lookup_contact_enrichment",
        lambda _number: {
            "contact_name": None,
            "first_name": None,
            "last_name": None,
            "company": None,
            "job_title": None,
            "status": "not_found",
            "degraded": False,
            "degraded_reason": None,
        },
    )
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", False)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda _text: True)

    hook_calls = []
    sms_calls = []

    def _fake_hook(normalized_sms, line_display=None):
        hook_calls.append({"normalized_sms": normalized_sms, "line_display": line_display})
        return True, "http_200"

    def _fake_send_sms(to_numbers, message, from_number=None, infer_country_code=False):
        sms_calls.append(
            {
                "to_numbers": to_numbers,
                "message": message,
                "from_number": from_number,
                "infer_country_code": infer_country_code,
            }
        )
        return {"id": "msg-1", "message_status": "pending"}

    monkeypatch.setattr(webhook_server, "send_sms_to_openclaw_hooks", _fake_hook)
    monkeypatch.setattr(webhook_server, "dialpad_send_sms", _fake_send_sms)

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Do you have the same type of machine?",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    response = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert status["code"] == 200
    assert len(sms_calls) == 1
    assert sms_calls[0]["to_numbers"] == ["+14155550123"]
    assert sms_calls[0]["from_number"] == "+14155201316"
    assert "ShapeScale for Business Sales" in sms_calls[0]["message"]
    assert hook_calls[0]["normalized_sms"]["first_contact"]["identityState"] == "not_found"
    assert response["auto_reply_sent"] is True
    assert response["auto_reply_status"] == "accepted/queued"


@pytest.mark.parametrize("lookup_status", ["disabled", "not_applicable", "resolved"])
def test_should_send_proactive_reply_requires_unknown_lookup(monkeypatch, lookup_status):
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)

    normalized_event = {
        "event_type": "sms",
        "sender_number": "+14155550123",
        "recipient_number": "+14155201316",
        "first_contact": {
            "knownContact": False,
            "lookup": {
                "status": lookup_status,
                "degraded": False,
                "degradedReason": None,
            },
        },
    }

    assert webhook_server.should_send_proactive_reply(normalized_event) is False


def test_inbound_telegram_escapes_markdown_content(monkeypatch):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Unknown"}},
    )
    monkeypatch.setattr(
        webhook_server,
        "lookup_contact_enrichment",
        lambda _number: {
            "contact_name": "Jane_Doe",
            "status": "resolved",
            "degraded": False,
            "degraded_reason": None,
        },
    )
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_sms_to_openclaw_hooks", lambda *_args, **_kwargs: (True, "http_200"))

    telegram_messages = []
    monkeypatch.setattr(
        webhook_server,
        "send_to_telegram",
        lambda text: telegram_messages.append(text) or True,
    )

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Need _bold_ *now* [check] `code`",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    assert status["code"] == 200
    assert len(telegram_messages) == 1
    assert "Jane\\_Doe" in telegram_messages[0]
    assert "Need \\_bold\\_ \\*now\\* \\[check] \\`code\\`" in telegram_messages[0]


def test_inbound_sensitive_sms_filtered_for_hook_and_telegram(monkeypatch):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Unknown"}},
    )
    monkeypatch.setattr(
        webhook_server,
        "lookup_contact_enrichment",
        lambda _number: {
            "contact_name": "Capital One",
            "status": "resolved",
            "degraded": False,
            "degraded_reason": None,
        },
    )
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)

    hook_calls = []

    def _fake_hook(normalized_sms, line_display=None):
        hook_calls.append({"normalized_sms": normalized_sms, "line_display": line_display})
        return True, "http_200"

    telegram_messages = []
    monkeypatch.setattr(webhook_server, "send_sms_to_openclaw_hooks", _fake_hook)
    monkeypatch.setattr(
        webhook_server,
        "send_to_telegram",
        lambda text: telegram_messages.append(text) or True,
    )

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Your OTP code is 773311 for login.",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    response = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert status["code"] == 200
    assert hook_calls == []
    assert telegram_messages == []
    assert response["hook_status"] == "filtered_sensitive"
    assert response["inbound_alert_eligible"] is False
    assert response["inbound_alert_reason"] == "filtered_sensitive"
    assert response["telegram_status"] == "filtered_sensitive"


def test_inbound_shortcode_sms_filtered_for_hook_and_telegram(monkeypatch):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Unknown"}},
    )
    monkeypatch.setattr(
        webhook_server,
        "lookup_contact_enrichment",
        lambda _number: {
            "contact_name": "Unknown",
            "status": "not_found",
            "degraded": False,
            "degraded_reason": None,
        },
    )
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)

    hook_calls = []
    telegram_messages = []
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: hook_calls.append(
            {"normalized_sms": normalized_sms, "line_display": line_display}
        ) or (True, "http_200"),
    )
    monkeypatch.setattr(
        webhook_server,
        "send_to_telegram",
        lambda text: telegram_messages.append(text) or True,
    )

    payload = {
        "direction": "inbound",
        "from_number": "12345",
        "to_number": ["+14155201316"],
        "text": "Code 009821 to verify.",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    response = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert status["code"] == 200
    assert hook_calls == []
    assert telegram_messages == []
    assert response["hook_status"] == "filtered_shortcode"
    assert response["inbound_alert_eligible"] is False
    assert response["inbound_alert_reason"] == "filtered_shortcode"
    assert response["telegram_status"] == "filtered_shortcode"


def test_inbound_hook_and_telegram_paths_share_eligible_result(monkeypatch):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Unknown"}},
    )
    monkeypatch.setattr(
        webhook_server,
        "lookup_contact_enrichment",
        lambda _number: {
            "contact_name": "Jane Doe",
            "status": "resolved",
            "degraded": False,
            "degraded_reason": None,
        },
    )
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)

    hook_calls = []
    telegram_messages = []

    def _fake_hook(normalized_sms, line_display=None):
        hook_calls.append({"normalized_sms": normalized_sms, "line_display": line_display})
        return True, "http_200"

    monkeypatch.setattr(webhook_server, "send_sms_to_openclaw_hooks", _fake_hook)
    monkeypatch.setattr(
        webhook_server,
        "send_to_telegram",
        lambda text: telegram_messages.append(text) or True,
    )

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Inbound hello",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    response = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert status["code"] == 200
    assert len(hook_calls) == 1
    assert len(telegram_messages) == 1
    assert response["hook_forwarded"] is True
    assert response["inbound_alert_eligible"] is True
    assert response["inbound_alert_reason"] == "eligible"
    assert response["telegram_status"] == "sent"


def test_inbound_sms_hook_respects_disabled_config(monkeypatch):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Unknown"}},
    )
    monkeypatch.setattr(
        webhook_server,
        "lookup_contact_enrichment",
        lambda _number: {
            "contact_name": "Jane Doe",
            "status": "resolved",
            "degraded": False,
            "degraded_reason": None,
        },
    )
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_SMS_ENABLED", False)
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_TOKEN", "token-123")
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", False)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda _text: True)

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Inbound hello",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    response = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert status["code"] == 200
    assert response["hook_forwarded"] is False
    assert response["hook_status"] == "disabled_by_config"
