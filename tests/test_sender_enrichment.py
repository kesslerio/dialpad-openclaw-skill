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
    assert response["hook_forwarded"] is True
    assert response["sender_enrichment_status"] == "resolved"
    assert response["sender_enrichment_degraded"] is False


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
