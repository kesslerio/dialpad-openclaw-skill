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
import sms_sqlite


@pytest.fixture(autouse=True)
def _clear_emergency_opt_out_memory():
    webhook_server.sms_approval._EMERGENCY_OPT_OUT_MEMORY.clear()
    yield
    webhook_server.sms_approval._EMERGENCY_OPT_OUT_MEMORY.clear()


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


class _FakeCompletedProcess:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


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
                "phones": ["+14155550123"],
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


def test_lookup_contact_enrichment_401_degraded_and_cached_fallback(monkeypatch, tmp_path):
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
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
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

    assert status["code"] == 200
    normalized_sms = hook_calls[0]["normalized_sms"]
    assert normalized_sms["first_contact"]["lookup"]["degraded"] is True
    assert normalized_sms["first_contact"]["lookup"]["degradedReason"] == "expired_token"
    assert normalized_sms["sender"] == "Cached Person"
    assert normalized_sms["first_contact"]["knownContact"] is False
    assert normalized_sms["first_contact"]["keepBrief"] is False
    assert normalized_sms["first_contact"]["identityState"] == "degraded"
    assert normalized_sms["inbound_context"]["identityConfidence"] == "low"
    assert normalized_sms["inbound_context"]["contextDraftAllowed"] is False


def test_inbound_telegram_uses_enriched_sender(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
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
        lambda text, **_kwargs: telegram_messages.append(text) or True,
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


def test_inbound_webhook_hook_uses_enriched_sender(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
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
    telegram_messages = []
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)

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

    assert status["code"] == 200
    # The hook fired exactly once with the resolved-sender payload (the rich
    # forwarding/enrichment status now lives on the captured hook payload, not
    # the minimal ACK response body).
    assert len(hook_calls) == 1
    normalized_sms = hook_calls[0]["normalized_sms"]
    assert normalized_sms["sender"] == "Jane Doe"
    assert normalized_sms["first_contact"]["knownContact"] is True
    assert normalized_sms["first_contact"]["keepBrief"] is True
    assert normalized_sms["first_contact"]["identityState"] == "resolved"
    assert normalized_sms["first_contact"]["lookup"]["status"] == "resolved"
    assert normalized_sms["first_contact"]["lookup"]["degraded"] is False


def test_inbound_webhook_hook_marks_unknown_sender_first_contact_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
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
    telegram_messages = []
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)

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


def test_not_eligible_inbound_stales_pending_draft(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_RICH_SMS_DRAFTS_ENABLED", False)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", tmp_path / "approvals.db")
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Jane Doe"}},
    )
    monkeypatch.setattr(
        webhook_server,
        "lookup_contact_enrichment",
        lambda _number: {
            "contact_name": "Jane Doe",
            "first_name": "Jane",
            "last_name": "Doe",
            "company": "Example Co",
            "job_title": "Owner",
            "status": "resolved",
            "degraded": False,
            "degraded_reason": None,
        },
    )
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", False)
    hook_calls = []
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: hook_calls.append(normalized_sms) or (True, "http_200"),
    )

    conn = webhook_server.sms_approval.init_db()
    try:
        pending = webhook_server.sms_approval.create_draft(
            conn,
            thread_key="hook:dialpad:sms:14155550123:14155201316",
            customer_number="+14155550123",
            sender_number="+14155201316",
            draft_text="Old draft must stale when contact is now known.",
        )
    finally:
        conn.close()

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "I already spoke with someone.",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    assert status["code"] == 200

    # A non-eligible inbound creates no new draft and stales the pending one;
    # the eligibility/draft outcome now lives in the approvals DB, not the ACK body.
    conn = webhook_server.sms_approval.init_db()
    try:
        stale_draft = webhook_server.sms_approval.get_draft(conn, pending["draft_id"])
    finally:
        conn.close()
    assert stale_draft["status"] == webhook_server.sms_approval.STATUS_STALE
    assert stale_draft["invalidated_reason"] == "new_inbound_not_eligible"
    # The non-eligible inbound still forwards the operator card, but must create NO
    # new approvable draft — staling the old one must not be paired with a fresh
    # pending draft (regression guard).
    assert hook_calls, "operator card should still be forwarded"
    assert hook_calls[0]["auto_reply"]["draftId"] is None
    assert hook_calls[0]["auto_reply"]["status"] == "not_eligible"


def test_inbound_sales_sms_creates_approval_draft_on_first_contact(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", tmp_path / "approvals.db")
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
    telegram_messages = []
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)

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

    assert status["code"] == 200
    assert sms_calls == []
    auto_reply = hook_calls[0]["normalized_sms"]["auto_reply"]
    assert hook_calls[0]["normalized_sms"]["first_contact"]["identityState"] == "not_found"
    assert auto_reply["sent"] is False
    assert auto_reply["draftCreated"] is True
    assert auto_reply["status"] == "draft_created"
    assert auto_reply["draftId"]
    assert "SMS approval draft" in telegram_messages[0]
    assert "not sent" in telegram_messages[0]
    assert auto_reply["draftId"] in telegram_messages[0].replace("\\_", "_")
    assert "bin/approve_sms_draft.py" in telegram_messages[0]
    assert "--approval-token" in telegram_messages[0]


def test_inbound_sales_sms_creates_generic_draft_for_payload_contact(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_RICH_SMS_DRAFTS_ENABLED", False)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", tmp_path / "approvals.db")
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Payload Person"}},
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
    telegram_messages = []
    hook_calls = []
    sms_calls = []
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: (
            hook_calls.append({"normalized_sms": normalized_sms, "line_display": line_display}) or
            (True, "http_200")
        ),
    )
    monkeypatch.setattr(
        webhook_server,
        "dialpad_send_sms",
        lambda *args, **kwargs: sms_calls.append((args, kwargs)) or {"id": "msg-1"},
    )

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Can I know the difference between the consumer and business version?",
        "contact": {"name": "Payload Person"},
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    normalized_sms = hook_calls[0]["normalized_sms"]
    inbound_context = normalized_sms["inbound_context"]
    auto_reply = normalized_sms["auto_reply"]
    assert status["code"] == 200
    assert sms_calls == []
    assert normalized_sms["first_contact"]["identityState"] == "payload_contact"
    assert normalized_sms["first_contact"]["knownContact"] is False
    assert inbound_context["identityConfidence"] == "low"
    assert inbound_context["contextDraftAllowed"] is False
    assert inbound_context["genericDraftAllowed"] is True
    assert inbound_context["draftMode"] == "deterministic_fallback"
    assert "exact_phone_match" not in inbound_context["evidence"]
    assert auto_reply["draftCreated"] is True
    assert auto_reply["status"] == "draft_created"
    assert auto_reply["draftId"]
    assert auto_reply["message"].startswith("Hi there,")
    assert "Payload Person" not in auto_reply["message"]
    assert "SMS approval draft" in telegram_messages[0]
    assert "approval draft created (generic fallback)" in telegram_messages[0]


def test_recent_thread_link_issue_creates_rich_approval_draft(monkeypatch, tmp_path):
    sms_db = tmp_path / "sms.db"
    approval_db = tmp_path / "approvals.db"
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", approval_db)
    monkeypatch.setattr(sms_sqlite, "DB_PATH", sms_db)
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Gabriela Valle"}},
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
    now_ms = 1760000000000
    conn = sms_sqlite.init_db()
    try:
        sms_sqlite.store_message(
            conn,
            {
                "id": 2001,
                "direction": "outbound",
                "from_number": "+14155201316",
                "to_number": ["+15109125052"],
                "text": "You can grab a time here: bysha.pe/book-demo",
                "created_date": now_ms - 5 * 60 * 1000,
                "contact": {"name": "Gabriela Valle"},
            },
            is_new=False,
        )
        sms_sqlite.store_message(
            conn,
            {
                "id": 2003,
                "direction": "inbound",
                "from_number": "+15109125052",
                "to_number": ["+14155201316"],
                "text": "I tried https://customer.example.test/wrong",
                "created_date": now_ms - 2 * 60 * 1000,
                "contact": {"name": "Gabriela Valle"},
            },
            is_new=False,
        )
    finally:
        conn.close()

    telegram_messages = []
    hook_calls = []
    sms_calls = []
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: (
            hook_calls.append({"normalized_sms": normalized_sms, "line_display": line_display}) or
            (True, "http_200")
        ),
    )
    monkeypatch.setattr(
        webhook_server,
        "dialpad_send_sms",
        lambda *args, **kwargs: sms_calls.append((args, kwargs)) or {"id": "msg-1"},
    )

    payload = {
        "id": 2002,
        "direction": "inbound",
        "from_number": "+15109125052",
        "to_number": ["+14155201316"],
        "text": "The link doesn't work",
        "created_date": now_ms,
        "contact": {"name": "Gabriela Valle"},
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    normalized_sms = hook_calls[0]["normalized_sms"]
    inbound_context = normalized_sms["inbound_context"]
    auto_reply = normalized_sms["auto_reply"]
    assert status["code"] == 200
    assert sms_calls == []
    assert inbound_context["identityConfidence"] == "low"
    assert inbound_context["recency"]["state"] == "fresh"
    assert "local_sms_history" in inbound_context["evidence"]
    assert inbound_context["genericDraftAllowed"] is False
    assert inbound_context["richDraftAllowed"] is True
    assert inbound_context["richDraftBasis"] == "recent_thread_link"
    assert inbound_context["richDraftCategory"] == "link_issue"
    assert inbound_context["draftMode"] == "knowledge_backed"
    assert auto_reply["draftCreated"] is True
    assert auto_reply["status"] == "draft_created"
    assert auto_reply["draftId"]
    assert auto_reply["richReply"]["basis"] == "recent_thread_link"
    assert "bysha.pe/book-demo" in auto_reply["message"]
    assert "customer.example.test" not in auto_reply["message"]
    assert "SMS approval draft" in telegram_messages[0]
    assert "approval draft created" in telegram_messages[0]
    assert "ShapeScale knowledge" in telegram_messages[0]
    assert "bysha" in telegram_messages[0]
    assert "thanks for reaching ShapeScale for Business Sales" not in telegram_messages[0]


def test_recent_sms_thread_context_excludes_current_message(monkeypatch, tmp_path):
    sms_db = tmp_path / "sms.db"
    monkeypatch.setattr(sms_sqlite, "DB_PATH", sms_db)
    now_ms = 1760000000000
    conn = sms_sqlite.init_db()
    try:
        sms_sqlite.store_message(
            conn,
            {
                "id": 3001,
                "direction": "outbound",
                "from_number": "+14155201316",
                "to_number": ["+15109125052"],
                "text": "You can grab a time here: bysha.pe/book-demo",
                "created_date": now_ms - 5 * 60 * 1000,
            },
            is_new=False,
        )
        sms_sqlite.store_message(
            conn,
            {
                "id": 3002,
                "direction": "inbound",
                "from_number": "+15109125052",
                "to_number": ["+14155201316"],
                "text": "The link doesn't work",
                "created_date": now_ms,
            },
            is_new=True,
        )
    finally:
        conn.close()

    thread = webhook_server.lookup_recent_sms_thread(
        "+15109125052",
        current_dialpad_id=3002,
        current_timestamp_ms=now_ms,
    )

    assert len(thread) == 1
    assert thread[0]["direction"] == "outbound"
    assert "bysha.pe/book-demo" in thread[0]["text"]
    assert "doesn't work" not in thread[0]["text"]


def test_recent_sms_thread_context_filters_stale_and_wrong_line_links(monkeypatch, tmp_path):
    sms_db = tmp_path / "sms.db"
    monkeypatch.setattr(sms_sqlite, "DB_PATH", sms_db)
    now_ms = 1760000000000
    conn = sms_sqlite.init_db()
    try:
        sms_sqlite.store_message(
            conn,
            {
                "id": 3101,
                "direction": "outbound",
                "from_number": "+14159917155",
                "to_number": ["+15109125052"],
                "text": "Old support link: https://support.example.test/wrong",
                "created_date": now_ms - 5 * 60 * 1000,
            },
            is_new=False,
        )
        sms_sqlite.store_message(
            conn,
            {
                "id": 3102,
                "direction": "outbound",
                "from_number": "+14155201316",
                "to_number": ["+15109125052"],
                "text": "Stale sales link: https://stale.example.test/book",
                "created_date": now_ms - 20 * 24 * 60 * 60 * 1000,
            },
            is_new=False,
        )
        sms_sqlite.store_message(
            conn,
            {
                "id": 3103,
                "direction": "outbound",
                "from_number": "+14155201316",
                "to_number": ["+15109125052"],
                "text": "Fresh sales link: bysha.pe/book-demo",
                "created_date": now_ms - 5 * 60 * 1000,
            },
            is_new=False,
        )
    finally:
        conn.close()

    thread = webhook_server.lookup_recent_sms_thread(
        "+15109125052",
        current_dialpad_id=3104,
        current_timestamp_ms=now_ms,
        current_line_number="+14155201316",
    )

    thread_text = " ".join(item["text"] for item in thread)
    assert "bysha.pe/book-demo" in thread_text
    assert "support.example.test" not in thread_text
    assert "stale.example.test" not in thread_text


def test_product_question_uses_shapescale_knowledge_for_rich_draft(monkeypatch, tmp_path):
    approval_db = tmp_path / "approvals.db"
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", approval_db)
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Payload Person"}},
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
    monkeypatch.setattr(
        webhook_server,
        "lookup_shapescale_knowledge",
        lambda query: {
            "usable": True,
            "status": "ok",
            "text": "ShapeScale for Business supports client scans, a client results view, and practice workflows.",
        },
    )

    telegram_messages = []
    hook_calls = []
    sms_calls = []
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: (
            hook_calls.append({"normalized_sms": normalized_sms, "line_display": line_display}) or
            (True, "http_200")
        ),
    )
    monkeypatch.setattr(
        webhook_server,
        "dialpad_send_sms",
        lambda *args, **kwargs: sms_calls.append((args, kwargs)) or {"id": "msg-1"},
    )

    payload = {
        "id": 4001,
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "How does the business scanner work?",
        "contact": {"name": "Payload Person"},
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    normalized_sms = hook_calls[0]["normalized_sms"]
    inbound_context = normalized_sms["inbound_context"]
    auto_reply = normalized_sms["auto_reply"]
    message = auto_reply["message"]
    assert status["code"] == 200
    assert sms_calls == []
    assert inbound_context["draftMode"] == "knowledge_backed"
    assert inbound_context["richDraftBasis"] == "shapescale_knowledge"
    assert inbound_context["richDraftCategory"] == "product"
    assert auto_reply["draftCreated"] is True
    assert auto_reply["status"] == "draft_created"
    assert "client scans" in message
    assert "[" not in message
    assert "ShapeScale knowledge" in telegram_messages[0]


def test_product_question_falls_back_when_knowledge_unavailable(monkeypatch, tmp_path):
    approval_db = tmp_path / "approvals.db"
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", approval_db)
    monkeypatch.setattr(
        webhook_server,
        "lookup_shapescale_knowledge",
        lambda query: {"usable": False, "status": "empty", "text": ""},
    )
    normalized_event = {
        "event_type": "sms",
        "sender_number": "+14155550123",
        "recipient_number": "+14155201316",
        "text": "How does the business scanner work?",
        "first_contact": {
            "knownContact": False,
            "needsDraftReply": True,
            "lookup": {"status": "not_found", "degraded": False},
        },
    }

    rich_reply = webhook_server.build_rich_sms_reply(normalized_event)

    assert rich_reply["usable"] is False
    assert rich_reply["status"] == "knowledge_empty"
    assert webhook_server.should_send_proactive_reply(normalized_event) is True
    assert webhook_server.build_proactive_reply_message(normalized_event).startswith("Hi there, thanks for reaching")


def test_knowledge_lookup_extracts_answer_body_from_qmd_get(monkeypatch):
    # qmd search returns the matching doc's title/source snippet (NOT the answer);
    # qmd get returns the full doc whose body holds the actual answer.
    search_output = """qmd://shapescale-knowledge/support/help-articles/693708-pricing-home.md:1 #237f44
Title: What is the pricing for ShapeScale for Home?
Context: Curated ShapeScale knowledge base.
Score:  78%

@@ -1,3 @@
# What is the pricing for ShapeScale for Home?
"""
    get_output = """Folder Context: Curated ShapeScale knowledge base.
---

# What is the pricing for ShapeScale for Home?

Source: https://shapescalehelpcenter.gorgias.help/pricing-home-693708?isEmbedded=true
Article ID: 693708
Category: Pricing & Shipping
Last updated: 2026-02-03T10:32:07.289Z

---

ShapeScale's home device is priced at $1,799 upfront for the hardware, plus an app subscription that only starts billing once you receive your unit.
"""
    calls = []

    def _fake_run(args, **kwargs):
        calls.append(args)
        stdout = search_output if args[1] == "search" else get_output
        return _FakeCompletedProcess(stdout=stdout, returncode=0)

    monkeypatch.setattr(webhook_server, "DIALPAD_QMD_COMMAND", "qmd")
    monkeypatch.setattr(webhook_server.subprocess, "run", _fake_run)

    result = webhook_server.lookup_shapescale_knowledge("how much does ShapeScale cost")

    assert result["usable"] is True
    assert result["status"] == "ok"
    # The answer body is returned, not the title/source/metadata.
    assert "$1,799" in result["text"]
    assert "What is the pricing" not in result["text"]
    assert "qmd://" not in result["text"]
    assert "Source:" not in result["text"]
    assert "Score:" not in result["text"]
    # get was called with the search hit's ref, keeping :line (to fetch the matched
    # section of long docs) and dropping the #hash.
    assert calls[0][1] == "search"
    assert calls[1][1] == "get"
    assert calls[1][2] == "qmd://shapescale-knowledge/support/help-articles/693708-pricing-home.md:1"


def test_qmd_command_returns_timeout_when_deadline_already_passed():
    # The shared deadline means a second call after the budget is spent fails fast
    # instead of starting a fresh full-timeout subprocess (no 2x worst-case hold).
    import time as _time
    out, status = webhook_server._run_qmd_command("qmd", ["search", "x"], _time.monotonic() - 1)
    assert (out, status) == ("", "timeout")


def test_knowledge_lookup_no_match_when_search_returns_no_hit(monkeypatch):
    monkeypatch.setattr(webhook_server, "DIALPAD_QMD_COMMAND", "qmd")
    monkeypatch.setattr(
        webhook_server.subprocess,
        "run",
        lambda *a, **k: _FakeCompletedProcess(stdout="No results found.", returncode=0),
    )
    result = webhook_server.lookup_shapescale_knowledge("how much does ShapeScale cost")
    assert result["usable"] is False
    assert result["status"] == "no_match"


def test_qmd_answer_body_strips_preamble_and_top_hit_ref_keeps_line():
    # The matched :line is kept (so qmd get fetches the right section); only #hash drops.
    assert webhook_server._qmd_top_hit_ref(
        "qmd://shapescale-knowledge/a/b.md:42 #deadbe\nTitle: x"
    ) == "qmd://shapescale-knowledge/a/b.md:42"
    assert webhook_server._qmd_top_hit_ref("Title: nothing here") is None
    # Paths with spaces (Notion/training exports) must survive — strip only the hash.
    assert webhook_server._qmd_top_hit_ref(
        "qmd://shapescale-knowledge/online training/intro to scan.md:7 #abc123"
    ) == "qmd://shapescale-knowledge/online training/intro to scan.md:7"
    body = webhook_server._qmd_answer_body(
        "Folder Context: ctx\n---\n# Heading\nSource: https://x\nArticle ID: 1\n---\nThe real answer is 42."
    )
    assert body == "The real answer is 42."


def test_qmd_answer_body_keeps_labeled_answer_lines():
    # A labeled answer line (label is NOT a known metadata key) must survive; only
    # recognized metadata keys are dropped.
    doc = "Source: https://x\n---\nPricing: $1,799 upfront for the hardware.\nNote: billing starts after delivery."
    body = webhook_server._qmd_answer_body(doc)
    assert "Pricing: $1,799 upfront for the hardware." in body
    assert "Note: billing starts after delivery." in body


def test_qmd_answer_body_strips_notion_metadata_block():
    # Notion exports use different metadata keys than Gorgias help articles; the
    # generic leading-block strip must drop them all, not just a fixed denylist.
    notion = (
        "Source course: Online Training\n"
        "Source page ID: 312746d6-e6c2\n"
        "Source URL: https://www.notion.so/Online-Training-312746d6\n"
        "Created: 2026-02-25T00:28:00.000Z\n"
        "\n"
        "ShapeScale is accurate down to 1/20th of an inch."
    )
    assert webhook_server._qmd_answer_body(notion) == "ShapeScale is accurate down to 1/20th of an inch."


def test_qmd_answer_body_strips_trailing_and_lowercase_metadata():
    # Metadata that re-appears AFTER the first prose line (transcluded/footer) or
    # uses lowercase keys must not leak internal URLs / record IDs into the draft.
    doc = (
        "---\n# Title\nSource: https://help.example/foo\n---\n"
        "The first prose line is fine.\n"
        "source: https://internal.shapescale.io/admin/leads\n"
        "Article ID: 8675309\n"
        "Last updated: 2026-06-19\n"
    )
    body = webhook_server._qmd_answer_body(doc)
    assert body == "The first prose line is fine."
    assert "internal.shapescale.io" not in body
    assert "8675309" not in body


def test_qmd_answer_body_strips_image_embeds_and_urls():
    # Verified production-shape leak: Gorgias bodies carry ![](attachment.png) image
    # embeds, <https://...> angle URLs, and bare URLs that must not reach the SMS.
    doc = (
        "---\n# Title\n---\n"
        "See the difference ![](https://attachments.gorgias.help/abc/photo.png) here.\n"
        "More at <https://business.shapescale.com/demo> or https://shapescale.com/x.\n"
    )
    body = webhook_server._qmd_answer_body(doc)
    assert "http" not in body
    assert "gorgias.help" not in body
    assert "See the difference" in body and "here." in body


def test_knowledge_query_is_deterministic_and_dedupes_anchor():
    # Equal-length ties must resolve the same way every call (no hash-seed drift),
    # and the anchor's own words must not be duplicated into the query.
    q1 = webhook_server._knowledge_query_for_category("product", "refund cancel policy return delays")
    q2 = webhook_server._knowledge_query_for_category("product", "refund cancel policy return delays")
    assert q1 == q2
    booking = webhook_server._knowledge_query_for_category("booking", "how do I book a demo")
    assert booking.split().count("book") == 1
    assert booking.split().count("demo") == 1


def test_knowledge_query_extracts_salient_keywords_for_and_search():
    # qmd search is AND-based, so the query must be a few high-signal content words,
    # not the full sentence (which matches nothing). Stopwords + brand are dropped.
    assert webhook_server._knowledge_query_for_category(
        "pricing", "how much does ShapeScale for home cost"
    ) == "pricing home"
    q = webhook_server._knowledge_query_for_category("product", "how does the business scanner work")
    assert set(q.split()) == {"business", "scanner"}
    # No anchor + no salient keywords -> empty query so the lookup fails closed
    # (rather than searching the brand alone and matching an arbitrary doc).
    assert webhook_server._knowledge_query_for_category("product", "how does it work") == ""
    # An anchored category still yields a query even with all-stopword text.
    assert webhook_server._knowledge_query_for_category("pricing", "how much does it cost") == "pricing"


def test_failed_rich_lookup_is_cached_for_generic_fallback(monkeypatch):
    calls = []

    def _lookup(_query):
        calls.append(_query)
        return {"usable": False, "status": "timeout", "text": ""}

    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(webhook_server, "lookup_shapescale_knowledge", _lookup)
    normalized_event = {
        "event_type": "sms",
        "sender_number": "+14155550123",
        "recipient_number": "+14155201316",
        "text": "How does the business scanner work?",
        "first_contact": {
            "knownContact": False,
            "needsDraftReply": True,
            "lookup": {"status": "not_found", "degraded": False},
        },
    }

    assert webhook_server.should_send_proactive_reply(normalized_event) is True
    assert normalized_event["rich_reply"]["status"] == "knowledge_timeout"
    assert webhook_server.build_proactive_reply_message(normalized_event).startswith("Hi there, thanks for reaching")
    assert len(calls) == 1


def test_known_sales_sms_creates_crm_aware_approval_draft(monkeypatch, tmp_path):
    sms_db = tmp_path / "sms.db"
    approval_db = tmp_path / "approvals.db"
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", approval_db)
    monkeypatch.setattr(sms_sqlite, "DB_PATH", sms_db)
    monkeypatch.setattr(
        webhook_server,
        "lookup_contact_enrichment",
        lambda _number: {
            "contact_name": "Gabriela Valle (Evolve from within medspa)",
            "first_name": "Gabriela",
            "last_name": "Valle",
            "company": "Evolve from within medspa",
            "job_title": None,
            "status": "resolved",
            "degraded": False,
            "degraded_reason": None,
        },
    )
    monkeypatch.setattr(
        webhook_server,
        "lookup_sales_crm_context",
        lambda *_args, **_kwargs: {
            "usable": True,
            "status": "ok",
            "basis": "attio",
            "company": "Evolve from within medspa",
            "deal": "ShapeScale demo",
            "stage": "Demo Scheduled",
            "owner": "Martin",
            "summary": "Demo Scheduled with Evolve from within medspa",
        },
    )
    monkeypatch.setattr(
        webhook_server,
        "lookup_sales_calendar_context",
        lambda *_args, **_kwargs: {"usable": False, "status": "not_applicable"},
    )

    now_ms = 1760000000000
    conn = sms_sqlite.init_db()
    try:
        sms_sqlite.store_message(
            conn,
            {
                "id": 5001,
                "direction": "outbound",
                "from_number": "+14155201316",
                "to_number": ["+15109125052"],
                "text": "Looking forward to our ShapeScale demo.",
                "created_date": now_ms - 60 * 60 * 1000,
                "contact": {"name": "Gabriela Valle"},
            },
            is_new=False,
        )
    finally:
        conn.close()

    telegram_messages = []
    hook_calls = []
    sms_calls = []
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: (
            hook_calls.append({"normalized_sms": normalized_sms, "line_display": line_display}) or
            (True, "http_200")
        ),
    )
    monkeypatch.setattr(
        webhook_server,
        "dialpad_send_sms",
        lambda *args, **kwargs: sms_calls.append((args, kwargs)) or {"id": "msg-1"},
    )

    payload = {
        "id": 5002,
        "direction": "inbound",
        "from_number": "+15109125052",
        "to_number": ["+14155201316"],
        "text": "Thanks, sounds good",
        "created_date": now_ms,
        "contact": {"name": "Gabriela Valle"},
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    normalized_sms = hook_calls[0]["normalized_sms"]
    inbound_context = normalized_sms["inbound_context"]
    auto_reply = normalized_sms["auto_reply"]
    message = auto_reply["message"]
    assert status["code"] == 200
    assert sms_calls == []
    assert inbound_context["draftMode"] == "crm_aware"
    assert inbound_context["richDraftBasis"] == "attio_crm"
    assert inbound_context["richDraftCategory"] == "crm_context"
    assert auto_reply["draftCreated"] is True
    assert auto_reply["status"] == "draft_created"
    assert auto_reply["draftId"]
    assert auto_reply["richReply"]["basis"] == "attio_crm"
    assert auto_reply["richReply"]["crmContext"]["company"] == "Evolve from within medspa"
    assert "raw" not in json.dumps(auto_reply["richReply"]).lower()
    assert "Evolve from within medspa" in message
    assert "recent ShapeScale conversation" not in message
    assert "CRM-aware" in telegram_messages[0]
    assert "SMS approval draft" in telegram_messages[0]


def test_running_late_sms_creates_meeting_aware_approval_draft(monkeypatch, tmp_path):
    sms_db = tmp_path / "sms.db"
    approval_db = tmp_path / "approvals.db"
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", approval_db)
    monkeypatch.setattr(sms_sqlite, "DB_PATH", sms_db)
    monkeypatch.setattr(
        webhook_server,
        "lookup_contact_enrichment",
        lambda _number: {
            "contact_name": "Gabriela Valle (Evolve from within medspa)",
            "first_name": "Gabriela",
            "last_name": "Valle",
            "company": "Evolve from within medspa",
            "job_title": None,
            "status": "resolved",
            "degraded": False,
            "degraded_reason": None,
        },
    )
    monkeypatch.setattr(
        webhook_server,
        "lookup_sales_crm_context",
        lambda *_args, **_kwargs: {
            "usable": True,
            "status": "ok",
            "basis": "attio",
            "company": "Evolve from within medspa",
            "deal": "ShapeScale demo",
            "stage": "Demo Scheduled",
            "owner": "Martin",
            "summary": "Demo Scheduled with Evolve from within medspa",
        },
    )
    calendar_calls = []

    def _calendar_context(*_args, **_kwargs):
        calendar_calls.append(_args)
        return {
            "usable": True,
            "status": "ok",
            "basis": "google_calendar",
            "summary": "ShapeScale Demo - Evolve from within medspa",
            "startsInMinutes": 0,
        }

    monkeypatch.setattr(webhook_server, "lookup_sales_calendar_context", _calendar_context)

    now_ms = 1760000000000
    conn = sms_sqlite.init_db()
    try:
        sms_sqlite.store_message(
            conn,
            {
                "id": 5101,
                "direction": "outbound",
                "from_number": "+14155201316",
                "to_number": ["+15109125052"],
                "text": "See you on the demo shortly.",
                "created_date": now_ms - 20 * 60 * 1000,
                "contact": {"name": "Gabriela Valle"},
            },
            is_new=False,
        )
    finally:
        conn.close()

    telegram_messages = []
    hook_calls = []
    sms_calls = []
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: (
            hook_calls.append({"normalized_sms": normalized_sms, "line_display": line_display}) or
            (True, "http_200")
        ),
    )
    monkeypatch.setattr(
        webhook_server,
        "dialpad_send_sms",
        lambda *args, **kwargs: sms_calls.append((args, kwargs)) or {"id": "msg-1"},
    )

    payload = {
        "id": 5102,
        "direction": "inbound",
        "from_number": "+15109125052",
        "to_number": ["+14155201316"],
        "text": "I'm running 5 min late",
        "created_date": now_ms,
        "contact": {"name": "Gabriela Valle"},
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    normalized_sms = hook_calls[0]["normalized_sms"]
    inbound_context = normalized_sms["inbound_context"]
    auto_reply = normalized_sms["auto_reply"]
    message = auto_reply["message"]
    assert status["code"] == 200
    assert sms_calls == []
    assert calendar_calls
    assert inbound_context["draftMode"] == "meeting_aware"
    assert inbound_context["richDraftBasis"] == "calendar_meeting"
    assert inbound_context["richDraftCategory"] == "meeting_logistics"
    assert auto_reply["status"] == "draft_created"
    assert auto_reply["richReply"]["basis"] == "calendar_meeting"
    assert auto_reply["richReply"]["calendarContext"]["summary"] == "ShapeScale Demo - Evolve from within medspa"
    assert "no worries" in message.lower()
    assert "thanks for letting me know" in message.lower()
    assert "recent ShapeScale conversation" not in message
    assert "meeting-aware" in telegram_messages[0]


def test_meeting_logistics_without_calendar_match_falls_back_safely(monkeypatch):
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    calendar_calls = []
    normalized_event = {
        "event_type": "sms",
        "sender_number": "+15109125052",
        "recipient_number": "+14155201316",
        "text": "I'm running 5 min late",
        "timestamp": 1760000000000,
        "first_contact": {
            "knownContact": True,
            "needsDraftReply": True,
            "contactName": "Gabriela Valle",
            "lookup": {"status": "resolved", "degraded": False},
        },
        "inbound_context": {
            "identityConfidence": "high",
            "contextDraftAllowed": True,
            "recency": {"state": "fresh", "source": "local_sms_history"},
        },
    }
    monkeypatch.setattr(
        webhook_server,
        "lookup_sales_crm_context",
        lambda *_args, **_kwargs: {"usable": True, "status": "ok", "basis": "attio", "company": "Evolve"},
    )

    def _calendar_context(*_args, **_kwargs):
        calendar_calls.append(_args)
        return {"usable": False, "status": "not_found"}

    monkeypatch.setattr(webhook_server, "lookup_sales_calendar_context", _calendar_context)

    rich_reply = webhook_server.build_rich_sms_reply(normalized_event)

    assert calendar_calls
    assert rich_reply["usable"] is False
    assert rich_reply["status"] == "calendar_not_found"
    assert webhook_server.should_send_proactive_reply(normalized_event) is True
    assert "recent ShapeScale conversation" in webhook_server.build_proactive_reply_message(normalized_event)


def test_model_draft_uses_compact_tool_facts_for_crm_reply(monkeypatch):
    captured = {}

    def _draft_model(args, **kwargs):
        captured["args"] = args
        captured["facts"] = json.loads(kwargs["input"])
        return _FakeCompletedProcess(
            stdout=json.dumps(
                {
                    "message": (
                        "Hi Dr. Chris, sorry I missed your call. I saw your ShapeScale demo request "
                        "for White House Chiropractic and booking may not have gone through. You can "
                        "grab a time here: https://bysha.pe/book-demo."
                    )
                }
            )
        )

    monkeypatch.setattr(webhook_server, "DIALPAD_DRAFT_MODEL_COMMAND", "/usr/bin/fake-draft-model")
    monkeypatch.setattr(webhook_server.draft_model.subprocess, "run", _draft_model)

    normalized_event = {
        "event_type": "missed_call",
        "sender_number": "+16155574482",
        "recipient_number": "+14155201316",
        "text": "",
        "line_display": "Sales",
        "inbound_context": {
            "identityConfidence": "high",
            "contextDraftAllowed": True,
        },
        "crm_context": {
            "usable": True,
            "status": "ok",
            "basis": "attio",
            "company": "White House Chiropractic",
            "deal": "ShapeScale demo",
            "stage": "Demo Request",
            "email": "drchris@example.test",
            "summary": "Demo Request with White House Chiropractic",
        },
        "calendar_context": {
            "usable": False,
            "status": "not_found",
            "basis": "google_calendar",
            "summary": "No scheduled demo found",
        },
        "comms_context": {
            "usable": True,
            "status": "ok",
            "basis": "prior_comms",
            "summary": "SMS: 2 outbound, 0 inbound; booking link sent 2x; latest Jun 22",
            "smsStatus": "usable",
            "gmailStatus": "not_found",
        },
    }

    rich_reply = webhook_server.build_contextual_sales_sms_reply(
        normalized_event,
        sender_enrichment={"first_name": "Dr. Chris"},
    )

    assert captured["args"] == ["/usr/bin/fake-draft-model"]
    facts = captured["facts"]
    assert facts["fallbackMessage"].startswith("Hi Dr. Chris, sorry we missed your call")
    assert facts["sources"]["crm"]["company"] == "White House Chiropractic"
    assert facts["sources"]["calendar"]["status"] == "not_found"
    assert facts["sources"]["comms"]["summary"].startswith("SMS: 2 outbound")
    assert facts["candidate"]["basis"] == "attio_crm"
    assert "secret-crm-record" not in json.dumps(facts).lower()
    assert rich_reply["basis"] == "model_attio_crm"
    assert rich_reply["modelDraft"]["status"] == "ok"
    assert rich_reply["modelDraft"]["fallbackBasis"] == "attio_crm"
    assert rich_reply["message"].startswith("Hi Dr. Chris, sorry I missed your call")


def test_model_draft_fails_closed_on_unsafe_output(monkeypatch):
    monkeypatch.setattr(webhook_server, "DIALPAD_DRAFT_MODEL_COMMAND", "/usr/bin/fake-draft-model")
    monkeypatch.setattr(
        webhook_server.draft_model.subprocess,
        "run",
        lambda *_args, **_kwargs: _FakeCompletedProcess(
            stdout=json.dumps({"message": "Hi Dr. Chris, I saw your Gmail and your demo is scheduled for tomorrow."})
        ),
    )

    normalized_event = {
        "event_type": "missed_call",
        "sender_number": "+16155574482",
        "recipient_number": "+14155201316",
        "text": "",
        "inbound_context": {
            "identityConfidence": "high",
            "contextDraftAllowed": True,
        },
        "crm_context": {
            "usable": True,
            "status": "ok",
            "basis": "attio",
            "company": "White House Chiropractic",
            "deal": "ShapeScale demo",
            "stage": "Demo Request",
        },
        "calendar_context": {
            "usable": False,
            "status": "not_found",
            "basis": "google_calendar",
        },
    }

    rich_reply = webhook_server.build_contextual_sales_sms_reply(
        normalized_event,
        sender_enrichment={"first_name": "Dr. Chris"},
    )

    assert rich_reply["basis"] == "attio_crm"
    assert rich_reply["modelDraft"]["status"] == "unsafe_output"
    assert "booking may not have gone through" in rich_reply["message"]
    assert "Gmail" not in rich_reply["message"]
    assert "scheduled for tomorrow" not in rich_reply["message"]


def test_model_draft_omits_low_confidence_crm_facts(monkeypatch):
    captured = {}

    def _draft_model(_args, **kwargs):
        captured["facts"] = json.loads(kwargs["input"])
        return _FakeCompletedProcess(stdout=json.dumps({"message": "Hi there, sorry we missed your call. We'll follow up shortly."}))

    monkeypatch.setattr(webhook_server, "DIALPAD_DRAFT_MODEL_COMMAND", "/usr/bin/fake-draft-model")
    monkeypatch.setattr(webhook_server.draft_model.subprocess, "run", _draft_model)

    normalized_event = {
        "event_type": "missed_call",
        "sender_number": "+16155574482",
        "recipient_number": "+14155201316",
        "text": "",
        "inbound_context": {
            "identityConfidence": "medium",
            "contextDraftAllowed": True,
        },
        "crm_context": {
            "usable": True,
            "status": "ok",
            "basis": "attio",
            "company": "White House Chiropractic",
            "deal": "ShapeScale demo",
            "stage": "Demo Request",
            "summary": "Demo Request with White House Chiropractic",
        },
        "calendar_context": {
            "usable": False,
            "status": "not_found",
            "basis": "google_calendar",
        },
    }

    rich_reply = webhook_server.build_contextual_sales_sms_reply(normalized_event)

    assert captured["facts"]["event"]["identityConfidence"] == "medium"
    assert captured["facts"]["sources"]["crm"] == {}
    assert "White House Chiropractic" not in json.dumps(captured["facts"])
    assert rich_reply["basis"] == "model_attio_crm"
    assert rich_reply["message"].startswith("Hi there")


def test_model_draft_rejects_internal_tool_names(monkeypatch):
    monkeypatch.setattr(webhook_server, "DIALPAD_DRAFT_MODEL_COMMAND", "/usr/bin/fake-draft-model")
    monkeypatch.setattr(
        webhook_server.draft_model.subprocess,
        "run",
        lambda *_args, **_kwargs: _FakeCompletedProcess(
            stdout=json.dumps({"message": "Hi Dr. Chris, based on Attio and CRM, booking may not have gone through."})
        ),
    )

    normalized_event = {
        "event_type": "missed_call",
        "sender_number": "+16155574482",
        "recipient_number": "+14155201316",
        "text": "",
        "inbound_context": {
            "identityConfidence": "high",
            "contextDraftAllowed": True,
        },
        "crm_context": {
            "usable": True,
            "status": "ok",
            "basis": "attio",
            "company": "White House Chiropractic",
            "deal": "ShapeScale demo",
            "stage": "Demo Request",
        },
        "calendar_context": {
            "usable": False,
            "status": "not_found",
            "basis": "google_calendar",
        },
    }

    rich_reply = webhook_server.build_contextual_sales_sms_reply(
        normalized_event,
        sender_enrichment={"first_name": "Dr. Chris"},
    )

    assert rich_reply["basis"] == "attio_crm"
    assert rich_reply["modelDraft"]["status"] == "unsafe_output"
    assert "Attio" not in rich_reply["message"]
    assert "CRM" not in rich_reply["message"]


def test_sales_context_lookups_store_only_compact_fields(monkeypatch):
    normalized_event = {
        "event_type": "sms",
        "sender_number": "+15109125052",
        "text": "I'm running 5 min late",
        "timestamp": 1760000000000,
        "inbound_context": {
            "identityConfidence": "high",
            "contextDraftAllowed": True,
        },
    }
    sender_enrichment = {
        "contact_name": "Gabriela Valle",
        "company": "Evolve from within medspa",
    }
    raw_results = [
        {
            "usable": True,
            "status": "ok",
            "basis": "attio",
            "company": "Evolve from within medspa",
            "deal": "ShapeScale demo",
            "stage": "Demo Scheduled",
            "owner": "Martin",
            "email": "gabriela@example.test",
            "summary": "Demo Scheduled",
            "raw": {"internal_id": "secret-crm-record"},
        },
        {
            "usable": True,
            "status": "ok",
            "basis": "google_calendar",
            "summary": "ShapeScale Demo - Evolve from within medspa",
            "startsInMinutes": 0,
            "attendees": ["internal@example.com"],
            "raw": {"calendar_id": "secret-calendar"},
        },
    ]

    command_queries = []

    def _context_command(_command, query):
        command_queries.append(query)
        return raw_results.pop(0)

    monkeypatch.setattr(webhook_server, "_run_context_command", _context_command)

    crm_context = webhook_server.lookup_sales_crm_context(normalized_event, sender_enrichment=sender_enrichment)
    calendar_context = webhook_server.lookup_sales_calendar_context(
        normalized_event,
        crm_context=crm_context,
        sender_enrichment=sender_enrichment,
    )

    assert crm_context == {
        "usable": True,
        "status": "ok",
        "basis": "attio",
        "company": "Evolve from within medspa",
        "deal": "ShapeScale demo",
        "stage": "Demo Scheduled",
        "owner": "Martin",
        "email": "gabriela@example.test",
        "summary": "Demo Scheduled ShapeScale demo Demo Scheduled Evolve from within medspa",
    }
    assert calendar_context == {
        "usable": True,
        "status": "ok",
        "basis": "google_calendar",
        "summary": "ShapeScale Demo - Evolve from within medspa",
        "startsInMinutes": 0,
        "demoState": None,
    }
    assert "gabriela@example.test" in command_queries[1]
    assert "secret" not in json.dumps({"crm": crm_context, "calendar": calendar_context})


def test_sales_comms_context_summarizes_sms_and_gmail_without_message_bodies(monkeypatch, tmp_path):
    db_path = tmp_path / "sms.db"
    monkeypatch.setattr(sms_sqlite, "DB_PATH", db_path)
    monkeypatch.setattr(webhook_server, "init_sms_history_db", sms_sqlite.init_db)
    conn = sms_sqlite.init_db()
    try:
        conn.execute(
            """
            INSERT INTO messages (
                contact_number, direction, from_number, to_number, text, timestamp, message_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "+16155574482",
                "outbound",
                "+14155201316",
                "+16155574482",
                "Looks like the demo booking did not finish. https://bysha.pe/book-demo",
                1760000000000 - 1000,
                "pending",
            ),
        )
        conn.execute(
            """
            INSERT INTO messages (
                contact_number, direction, from_number, to_number, text, timestamp, message_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "+16155574482",
                "outbound",
                "+14155201316",
                "+16155574482",
                "Second private booking-link follow-up https://bysha.pe/book-demo",
                1760000000000 - 500,
                "pending",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    gmail_queries = []

    def _fake_run(args, **_kwargs):
        gmail_queries.append(args[3])
        return _FakeCompletedProcess(
            stdout=json.dumps([
                {
                    "date": "2026-06-22 09:00",
                    "from": "Dr Chris <drchris@example.test>",
                    "subject": "Private subject should not be surfaced",
                }
            ])
        )

    monkeypatch.setattr(webhook_server, "DIALPAD_GMAIL_CONTEXT_COMMAND", "/bin/gog-shapescale")
    monkeypatch.setattr(webhook_server.subprocess, "run", _fake_run)

    ctx = webhook_server.lookup_sales_comms_context(
        {
            "event_type": "missed_call",
            "sender_number": "+16155574482",
            "recipient_number": "+14155201316",
            "timestamp": 1760000000000,
        },
        crm_context={
            "usable": True,
            "stage": "Demo Request",
            "company": "White House Chiropractic",
            "email": "drchris@example.test",
        },
        sender_enrichment={"contact_name": "Dr Chris"},
    )

    assert ctx["usable"] is True
    assert ctx["basis"] == "prior_comms"
    assert ctx["smsOutboundCount"] == 2
    assert ctx["smsInboundCount"] == 0
    assert ctx["smsBookingLinkCount"] == 2
    assert ctx["gmailMessageCount"] == 1
    assert "SMS: 2 outbound, 0 inbound" in ctx["summary"]
    assert "booking link sent 2x" in ctx["summary"]
    assert "Gmail: 1 exact-match message" in ctx["summary"]
    assert "Private subject" not in json.dumps(ctx)
    assert "drchris@example.test" in gmail_queries[0]


def test_sales_comms_context_not_applicable_without_demo_missed_call(monkeypatch):
    ctx = webhook_server.lookup_sales_comms_context(
        {"event_type": "sms", "sender_number": "+16155574482", "recipient_number": "+14155201316"},
        crm_context={"usable": True, "stage": "Demo Request"},
    )
    assert ctx == {"usable": False, "status": "not_applicable"}


def test_gmail_comms_search_does_not_use_contact_name_only(monkeypatch):
    calls = []
    monkeypatch.setattr(webhook_server, "DIALPAD_GMAIL_CONTEXT_COMMAND", "/bin/gog-shapescale")
    monkeypatch.setattr(
        webhook_server.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(args) or _FakeCompletedProcess(stdout="[]"),
    )

    gmail = webhook_server._summarize_gmail_comms(
        crm_context={"usable": True, "stage": "Demo Request"},
        sender_enrichment={"contact_name": "Dr Chris"},
    )

    assert gmail["status"] == "empty_query"
    assert calls == []


def test_sales_context_lookup_failures_store_only_status(monkeypatch):
    normalized_event = {
        "event_type": "sms",
        "sender_number": "+15109125052",
        "text": "I'm running 5 min late",
        "timestamp": 1760000000000,
        "inbound_context": {
            "identityConfidence": "high",
            "contextDraftAllowed": True,
        },
    }
    raw_failures = [
        {"usable": False, "status": "not_found", "raw": {"internal_id": "secret-crm-record"}},
        {"usable": False, "status": "not_found", "raw": {"calendar_id": "secret-calendar"}},
    ]

    monkeypatch.setattr(webhook_server, "_run_context_command", lambda *_args: raw_failures.pop(0))

    crm_context = webhook_server.lookup_sales_crm_context(normalized_event)
    calendar_context = webhook_server.lookup_sales_calendar_context(normalized_event)

    assert crm_context == {"usable": False, "status": "not_found"}
    assert calendar_context == {"usable": False, "status": "not_found"}
    assert "secret" not in json.dumps({"crm": crm_context, "calendar": calendar_context})


def test_sales_crm_context_without_allowlisted_fields_fails_closed(monkeypatch):
    normalized_event = {
        "event_type": "sms",
        "sender_number": "+15109125052",
        "text": "Thanks",
        "inbound_context": {
            "identityConfidence": "high",
            "contextDraftAllowed": True,
        },
    }
    monkeypatch.setattr(
        webhook_server,
        "_run_context_command",
        lambda *_args: {
            "usable": True,
            "status": "ok",
            "raw": {"internal_id": "secret-crm-record"},
        },
    )

    crm_context = webhook_server.lookup_sales_crm_context(normalized_event)

    assert crm_context == {"usable": False, "status": "empty"}
    assert "secret" not in json.dumps(crm_context)


def test_sales_crm_context_rejects_nested_allowlisted_values(monkeypatch):
    normalized_event = {
        "event_type": "sms",
        "sender_number": "+15109125052",
        "text": "Thanks",
        "inbound_context": {
            "identityConfidence": "high",
            "contextDraftAllowed": True,
        },
    }
    monkeypatch.setattr(
        webhook_server,
        "_run_context_command",
        lambda *_args: {
            "usable": True,
            "status": "ok",
            "summary": {"raw": "secret-summary"},
            "company": {"name": "secret-company"},
            "deal": ["secret-deal"],
            "stage": {"name": "secret-stage"},
            "owner": {"name": "secret-owner"},
        },
    )

    crm_context = webhook_server.lookup_sales_crm_context(normalized_event)

    assert crm_context == {"usable": False, "status": "empty"}
    assert "secret" not in json.dumps(crm_context)


def test_sales_crm_context_compacts_scalar_allowlisted_values(monkeypatch):
    normalized_event = {
        "event_type": "sms",
        "sender_number": "+15109125052",
        "text": "Thanks",
        "inbound_context": {
            "identityConfidence": "high",
            "contextDraftAllowed": True,
        },
    }
    monkeypatch.setattr(
        webhook_server,
        "_run_context_command",
        lambda *_args: {
            "usable": True,
            "status": "ok",
            "summary": " Demo   scheduled ",
            "company": " Evolve from within medspa ",
            "deal": " ShapeScale demo ",
            "stage": " Demo Scheduled ",
            "owner": 12345,
            "email": " gabriela@example.test ",
        },
    )

    crm_context = webhook_server.lookup_sales_crm_context(normalized_event)

    assert crm_context["usable"] is True
    assert crm_context["company"] == "Evolve from within medspa"
    assert crm_context["deal"] == "ShapeScale demo"
    assert crm_context["stage"] == "Demo Scheduled"
    assert crm_context["owner"] == "12345"
    assert crm_context["email"] == "gabriela@example.test"
    assert crm_context["summary"] == "Demo scheduled ShapeScale demo Demo Scheduled Evolve from within medspa"


def test_sales_calendar_context_rejects_nested_summary(monkeypatch):
    normalized_event = {
        "event_type": "sms",
        "sender_number": "+15109125052",
        "text": "I'm running 5 min late",
        "timestamp": 1760000000000,
        "inbound_context": {
            "identityConfidence": "high",
            "contextDraftAllowed": True,
        },
    }
    monkeypatch.setattr(
        webhook_server,
        "_run_context_command",
        lambda *_args: {
            "usable": True,
            "status": "ok",
            "summary": {"raw": "secret-calendar"},
            "title": ["secret-title"],
            "startsInMinutes": {"raw": "secret-start"},
        },
    )

    calendar_context = webhook_server.lookup_sales_calendar_context(normalized_event)

    assert calendar_context == {"usable": False, "status": "empty"}
    assert "secret" not in json.dumps(calendar_context)


def test_sales_calendar_context_compacts_scalar_summary(monkeypatch):
    normalized_event = {
        "event_type": "sms",
        "sender_number": "+15109125052",
        "text": "I'm running 5 min late",
        "timestamp": 1760000000000,
        "inbound_context": {
            "identityConfidence": "high",
            "contextDraftAllowed": True,
        },
    }
    monkeypatch.setattr(
        webhook_server,
        "_run_context_command",
        lambda *_args: {
            "usable": True,
            "status": "ok",
            "title": " ShapeScale   Demo ",
            "startsInMinutes": {"raw": "secret-start"},
        },
    )

    calendar_context = webhook_server.lookup_sales_calendar_context(normalized_event)

    assert calendar_context == {
        "usable": True,
        "status": "ok",
        "basis": "google_calendar",
        "summary": "ShapeScale Demo",
        "startsInMinutes": None,
        "demoState": None,
    }
    assert "secret" not in json.dumps(calendar_context)


def test_context_command_rejects_non_object_json_payload(monkeypatch):
    class Completed:
        returncode = 0
        stdout = '[{"raw":"secret-record"}]'

    monkeypatch.setattr(webhook_server.subprocess, "run", lambda *_args, **_kwargs: Completed())

    result = webhook_server._run_context_command("context-command", "query")

    assert result == {"usable": False, "status": "invalid_payload"}
    assert "secret" not in json.dumps(result)


def test_known_recent_sales_sms_creates_context_approval_draft(monkeypatch, tmp_path):
    sms_db = tmp_path / "sms.db"
    approval_db = tmp_path / "approvals.db"
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", approval_db)
    monkeypatch.setattr(sms_sqlite, "DB_PATH", sms_db)
    monkeypatch.setattr(
        webhook_server,
        "lookup_contact_enrichment",
        lambda _number: {
            "contact_name": "Ann Harper",
            "first_name": "Ann",
            "last_name": "Harper",
            "company": "Prospect",
            "job_title": None,
            "status": "resolved",
            "degraded": False,
            "degraded_reason": None,
        },
    )
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)

    now_ms = 1760000000000
    conn = sms_sqlite.init_db()
    try:
        sms_sqlite.store_message(
            conn,
            {
                "id": 1001,
                "direction": "outbound",
                "from_number": "+14155201316",
                "to_number": ["+14322083277"],
                "text": "Prior ShapeScale follow-up.",
                "created_date": now_ms - (2 * 24 * 60 * 60 * 1000),
                "contact": {"name": "Ann Harper"},
            },
            is_new=False,
        )
    finally:
        conn.close()

    hook_calls = []
    telegram_messages = []
    sms_calls = []
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: (
            hook_calls.append({"normalized_sms": normalized_sms, "line_display": line_display}) or
            (True, "http_200")
        ),
    )
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)
    monkeypatch.setattr(
        webhook_server,
        "dialpad_send_sms",
        lambda *args, **kwargs: sms_calls.append((args, kwargs)) or {"id": "msg-1", "message_status": "pending"},
    )

    payload = {
        "id": 1002,
        "direction": "inbound",
        "from_number": "+14322083277",
        "to_number": ["+14155201316"],
        "text": "Can you call me?",
        "created_date": now_ms,
        "contact": {"name": "Ann Harper"},
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    normalized_sms = hook_calls[0]["normalized_sms"]
    inbound_context = normalized_sms["inbound_context"]
    auto_reply = normalized_sms["auto_reply"]
    assert status["code"] == 200
    assert sms_calls == []
    assert inbound_context["knownContact"] is True
    assert inbound_context["identityConfidence"] == "high"
    assert inbound_context["recency"]["state"] == "fresh"
    assert inbound_context["contextDraftAllowed"] is True
    assert "local_sms_history" in inbound_context["evidence"]
    assert auto_reply["draftCreated"] is True
    assert auto_reply["status"] == "draft_created"
    assert auto_reply["draftId"]
    assert "Inbound context" in telegram_messages[0]
    assert "Ann Harper" in telegram_messages[0]
    assert "SMS approval draft" in telegram_messages[0]


def test_inbound_opt_out_blocks_hooks_sends_and_invalidates_pending_drafts(monkeypatch, tmp_path):
    approval_db = tmp_path / "approvals.db"
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", approval_db)
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Unknown"}},
    )
    monkeypatch.setattr(
        webhook_server,
        "lookup_contact_enrichment",
        lambda _number: {
            "contact_name": "Lisa Primps (The Primping Place)",
            "first_name": "Lisa",
            "last_name": "Primps",
            "company": "The Primping Place",
            "job_title": None,
            "status": "resolved",
            "degraded": False,
            "degraded_reason": None,
        },
    )

    telegram_messages = []
    hook_calls = []
    sms_calls = []
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", False)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)
    monkeypatch.setattr(webhook_server, "send_sms_to_openclaw_hooks", lambda *args, **kwargs: hook_calls.append(args) or (True, "http_200"))
    monkeypatch.setattr(webhook_server, "dialpad_send_sms", lambda *args, **kwargs: sms_calls.append(args) or {"id": "msg-1"})

    conn = webhook_server.sms_approval.init_db()
    try:
        pending = webhook_server.sms_approval.create_draft(
            conn,
            thread_key="prior-thread",
            customer_number="+14155550123",
            sender_number="+14155201316",
            draft_text="Prior draft must not remain approvable.",
        )
    finally:
        conn.close()

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "I need a real person. Please don't bother me anymore.",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    assert status["code"] == 200
    # An opt-out inbound forwards nothing to hooks, sends no SMS, creates no draft,
    # and the operator gets a human-only Telegram alert. Those outcomes now live in
    # the captured side effects + approvals DB, not the minimal ACK body.
    assert hook_calls == []
    assert sms_calls == []
    assert "human-only" in telegram_messages[0]
    assert "Lisa Primps (The Primping Place) (+14155550123)" in telegram_messages[0]
    assert "Message: I need a real person. Please don't bother me anymore." in telegram_messages[0]

    conn = webhook_server.sms_approval.init_db()
    try:
        stale_draft = webhook_server.sms_approval.get_draft(conn, pending["draft_id"])
        opted_out = webhook_server.sms_approval.is_opted_out(conn, "+14155550123")
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM sms_approval_drafts WHERE customer_number = ? "
            "AND status IN (?, ?)",
            (
                "+14155550123",
                webhook_server.sms_approval.STATUS_PENDING,
                webhook_server.sms_approval.STATUS_RISK_PENDING,
            ),
        ).fetchone()[0]
    finally:
        conn.close()
    assert stale_draft["status"] == webhook_server.sms_approval.STATUS_STALE
    assert stale_draft["invalidated_reason"] == "customer_opt_out"
    assert opted_out is True
    # Opt-out stales the prior draft AND must not leave/create a new approvable one.
    assert pending_count == 0


def test_opt_out_persistence_failure_records_emergency_block(monkeypatch, tmp_path):
    approval_db = tmp_path / "approvals.db"
    emergency_path = tmp_path / "emergency-opt-outs.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_APPROVAL_EMERGENCY_PATH", str(emergency_path))
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", approval_db)
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
    # Capture the operator alert to distinguish a successful emergency-ledger block
    # from a total persistence failure (the sibling test pins "persistence failed").
    telegram_messages = []
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)

    conn = webhook_server.sms_approval.init_db()
    try:
        pending = webhook_server.sms_approval.create_draft(
            conn,
            thread_key="prior-thread",
            customer_number="+14155550123",
            sender_number="+14155201316",
            draft_text="Prior draft must not remain approvable.",
        )
    finally:
        conn.close()

    def _fail_mark_opt_out(*_args, **_kwargs):
        raise OSError("simulated read-only approval db")

    monkeypatch.setattr(webhook_server.sms_approval, "mark_opt_out", _fail_mark_opt_out)

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Please stop texting me.",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    assert status["code"] == 200
    # When the durable opt-out write fails, the emergency fail-closed ledger must
    # still record the block (the opt-out outcome no longer rides the ACK body).
    assert emergency_path.exists()
    # And the operator alert must report the opt-out BLOCK, not "persistence failed"
    # (the sibling total-failure case) — preserves the success/failure distinction.
    assert telegram_messages
    assert "persistence failed" not in telegram_messages[0]
    assert "opt-out / human-only" in telegram_messages[0]

    conn = webhook_server.sms_approval.init_db()
    try:
        stale_draft = webhook_server.sms_approval.get_draft(conn, pending["draft_id"])
        result = webhook_server.sms_approval.approve_draft(
            conn,
            draft_id=pending["draft_id"],
            actor_id="12345",
            send_func=lambda *_args, **_kwargs: pytest.fail("send should not run"),
        )
    finally:
        conn.close()
    assert stale_draft["status"] == webhook_server.sms_approval.STATUS_STALE
    assert result["sent"] is False


def test_opt_out_persistence_total_failure_reports_failure_status(monkeypatch, tmp_path):
    approval_db = tmp_path / "approvals.db"
    emergency_path = tmp_path / "emergency-opt-outs-dir"
    emergency_path.mkdir()
    monkeypatch.setenv("DIALPAD_SMS_APPROVAL_EMERGENCY_PATH", str(emergency_path))
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", approval_db)
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
    telegram_messages = []
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)

    conn = webhook_server.sms_approval.init_db()
    try:
        pending = webhook_server.sms_approval.create_draft(
            conn,
            thread_key="prior-thread",
            customer_number="+14155550123",
            sender_number="+14155201316",
            draft_text="Prior draft must not remain approvable.",
        )
    finally:
        conn.close()

    def _fail_mark_opt_out(*_args, **_kwargs):
        raise OSError("simulated read-only approval db")

    monkeypatch.setattr(webhook_server.sms_approval, "mark_opt_out", _fail_mark_opt_out)

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Please stop texting me.",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    assert status["code"] == 200
    # When both the durable write AND the emergency ledger fail, the operator must
    # be told persistence failed via Telegram (status no longer in the ACK body).
    assert "persistence failed" in telegram_messages[0]

    conn = webhook_server.sms_approval.init_db()
    try:
        stale_draft = webhook_server.sms_approval.get_draft(conn, pending["draft_id"])
        opted_out = webhook_server.sms_approval.is_opted_out(conn, "+14155550123")
    finally:
        conn.close()
    assert stale_draft["status"] == webhook_server.sms_approval.STATUS_STALE
    assert opted_out is True


def test_standard_stop_keyword_blocks_sms_automation(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", tmp_path / "approvals.db")
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
    telegram_messages = []
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)
    hook_calls = []
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda *args, **kwargs: hook_calls.append(args) or (True, "http_200"),
    )

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "STOPALL",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    assert status["code"] == 200
    # A standard STOP keyword blocks automation: nothing forwarded to hooks and the
    # customer is durably opted out (the filtered reason no longer rides the ACK body).
    assert hook_calls == []
    conn = webhook_server.sms_approval.init_db()
    try:
        opted_out = webhook_server.sms_approval.is_opted_out(conn, "+14155550123")
    finally:
        conn.close()
    assert opted_out is True


def test_opt_out_with_security_code_persists_opt_out_before_sensitive_filter(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", tmp_path / "approvals.db")
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
    telegram_messages = []
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)
    hook_calls = []
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: hook_calls.append(normalized_sms) or (True, "http_200"),
    )

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Your security code is 123456. Do not contact me.",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    conn = webhook_server.sms_approval.init_db()
    try:
        opted_out = webhook_server.sms_approval.is_opted_out(conn, "+14155550123")
    finally:
        conn.close()
    assert status["code"] == 200
    # The opt-out is persisted BEFORE the sensitive-content filter would suppress
    # the message, so the customer is durably opted out despite the security code.
    assert opted_out is True
    # OTP-leak guard: the security-code SMS must never be forwarded to OpenClaw hooks.
    assert hook_calls == []


def test_stop_by_phrase_does_not_create_permanent_opt_out(monkeypatch, tmp_path):
    hook_calls = []
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", tmp_path / "approvals.db")
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
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: hook_calls.append(normalized_sms) or (True, "http_200"),
    )

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Can we stop by later?",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    conn = webhook_server.sms_approval.init_db()
    try:
        opted_out = webhook_server.sms_approval.is_opted_out(conn, "+14155550123")
    finally:
        conn.close()
    assert status["code"] == 200
    # "stop by" is a benign phrase, not a STOP opt-out: the message is treated as
    # eligible (forwarded to hooks) and creates NO permanent opt-out.
    assert hook_calls
    assert opted_out is False


def test_second_inbound_without_conversation_id_invalidates_previous_draft(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", tmp_path / "approvals.db")
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
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda _text, **_kwargs: True)
    hook_calls = []
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: hook_calls.append(normalized_sms) or (True, "http_200"),
    )

    first = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "message_id": "msg-1",
        "text": "First question.",
    }
    first_handler, _first_status = _build_handler(first)
    webhook_server.DialpadWebhookHandler.handle_webhook(first_handler)
    # The first inbound's draft id now lives on the forwarded hook payload, not the ACK body.
    first_draft_id = hook_calls[0]["auto_reply"]["draftId"]
    assert first_draft_id

    second = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "message_id": "msg-2",
        "text": "Second question.",
    }
    second_handler, status = _build_handler(second)
    webhook_server.DialpadWebhookHandler.handle_webhook(second_handler)

    conn = webhook_server.sms_approval.init_db()
    try:
        stale = webhook_server.sms_approval.get_draft(conn, first_draft_id)
    finally:
        conn.close()
    assert status["code"] == 200
    assert stale["status"] == webhook_server.sms_approval.STATUS_STALE
    assert stale["invalidated_reason"] == "superseded_by_new_draft"


def test_outbound_sms_invalidates_pending_approval_draft(monkeypatch, tmp_path):
    approval_db = tmp_path / "approvals.db"
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", approval_db)
    monkeypatch.setattr(
        webhook_server,
        "handle_sms_webhook",
        lambda _data: {"stored": True, "message": {"contact_name": "Unknown"}},
    )
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda _text: True)
    hook_calls = []
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: hook_calls.append(normalized_sms) or (True, "http_200"),
    )

    conn = webhook_server.sms_approval.init_db()
    try:
        draft = webhook_server.sms_approval.create_draft(
            conn,
            thread_key="thread-1",
            customer_number="+14155550123",
            sender_number="+14155201316",
            draft_text="Pending draft.",
        )
        second_draft = webhook_server.sms_approval.create_draft(
            conn,
            thread_key="thread-2",
            customer_number="+14155550124",
            sender_number="+14155201316",
            draft_text="Second pending draft.",
        )
    finally:
        conn.close()

    payload = {
        "direction": "outbound",
        "from_number": "+14155201316",
        "to_number": ["+14155550123", "+14155550124"],
        "text": "Human replied.",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    conn = webhook_server.sms_approval.init_db()
    try:
        stale = webhook_server.sms_approval.get_draft(conn, draft["draft_id"])
        second_stale = webhook_server.sms_approval.get_draft(conn, second_draft["draft_id"])
    finally:
        conn.close()
    assert status["code"] == 200
    # A manual outbound SMS is never forwarded to hooks; its only effect is staling
    # every pending approval draft for the recipients (verified via the DB).
    assert stale["status"] == webhook_server.sms_approval.STATUS_STALE
    assert stale["invalidated_reason"] == "manual_outbound"
    assert second_stale["status"] == webhook_server.sms_approval.STATUS_STALE
    assert second_stale["invalidated_reason"] == "manual_outbound"
    # A manual outbound SMS is never forwarded to OpenClaw hooks.
    assert hook_calls == []


def test_risky_inbound_sales_sms_creates_two_step_approval_draft(monkeypatch, tmp_path):
    approval_db = tmp_path / "approvals.db"
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", approval_db)
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

    hook_calls = []
    sms_calls = []
    telegram_messages = []
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: hook_calls.append(normalized_sms) or (True, "http_200"),
    )
    monkeypatch.setattr(webhook_server, "dialpad_send_sms", lambda *args, **kwargs: sms_calls.append(args) or {"id": "msg-1"})

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "I need to talk to a real person about the meeting time.",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    assert status["code"] == 200
    assert sms_calls == []
    auto_reply = hook_calls[0]["auto_reply"]
    auto_reply_draft_id = auto_reply["draftId"]
    assert auto_reply["status"] == "draft_created"
    assert auto_reply_draft_id
    assert auto_reply["replyPolicy"]["state"] == "risky"
    assert "Second confirmation required" in telegram_messages[0]
    assert "Risk:" in telegram_messages[0]
    assert "--approval-token" in telegram_messages[0]

    conn = webhook_server.sms_approval.init_db()
    try:
        draft = webhook_server.sms_approval.get_draft(conn, auto_reply_draft_id)
    finally:
        conn.close()
    assert draft["risk_state"] == webhook_server.sms_approval.RISK_RISKY
    assert draft["status"] == webhook_server.sms_approval.STATUS_PENDING


def test_previously_opted_out_customer_gets_blocked_status_not_persistence_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", tmp_path / "approvals.db")
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
    telegram_messages = []
    hook_calls = []
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", True)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda text, **_kwargs: telegram_messages.append(text) or True)
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: hook_calls.append(normalized_sms) or (True, "http_200"),
    )

    conn = webhook_server.sms_approval.init_db()
    try:
        webhook_server.sms_approval.mark_opt_out(
            conn,
            customer_number="+14155550123",
            reason="customer_opt_out",
            source="test",
        )
    finally:
        conn.close()

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Can you answer one more question?",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    assert status["code"] == 200
    # Previously-opted-out customer: the auto-reply is blocked (no draft) because the
    # opt-out is already on file — NOT a persistence failure. The blocked outcome now
    # lives on the forwarded hook payload + operator Telegram card, not the ACK body.
    auto_reply = hook_calls[0]["auto_reply"]
    assert auto_reply["status"] == "blocked_opt_out"
    assert auto_reply["draftId"] is None
    assert auto_reply["replyPolicy"]["state"] == "blocked_opt_out"
    assert len(telegram_messages) == 1
    assert "Automation blocked" in telegram_messages[0]
    assert "human" in telegram_messages[0]
    assert "No SMS approval draft" in telegram_messages[0]



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


def test_should_send_proactive_reply_allows_payload_contact_sms_generic_draft(monkeypatch):
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)

    normalized_event = {
        "event_type": "sms",
        "sender_number": "+14155550123",
        "recipient_number": "+14155201316",
        "first_contact": {
            "knownContact": False,
            "needsDraftReply": True,
            "lookup": {
                "status": "payload_contact",
                "degraded": False,
                "degradedReason": None,
            },
        },
    }

    assert webhook_server.should_send_proactive_reply(normalized_event) is True


def test_should_send_proactive_reply_suppresses_generic_draft_for_active_thread(monkeypatch, tmp_path):
    sms_db = tmp_path / "sms.db"
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(sms_sqlite, "DB_PATH", sms_db)
    now_ms = 1760000000000
    conn = sms_sqlite.init_db()
    try:
        sms_sqlite.store_message(
            conn,
            {
                "id": "prior-outbound",
                "direction": "outbound",
                "from_number": "+14155201316",
                "to_number": ["+15109125052"],
                "text": "You can grab a time here: bysha.pe/book-demo",
                "created_date": now_ms - 5 * 60 * 1000,
            },
            is_new=False,
        )
    finally:
        conn.close()

    normalized_event = {
        "event_type": "sms",
        "message_id": "current-inbound",
        "timestamp": now_ms,
        "sender_number": "+15109125052",
        "recipient_number": "+14155201316",
        "first_contact": {
            "knownContact": False,
            "needsDraftReply": True,
            "lookup": {
                "status": "payload_contact",
                "degraded": False,
                "degradedReason": None,
            },
        },
    }

    assert webhook_server.should_send_proactive_reply(normalized_event) is False


def test_inbound_telegram_escapes_markdown_content(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
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
        lambda text, **_kwargs: telegram_messages.append(text) or True,
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


def test_inbound_sensitive_sms_filtered_for_hook_and_telegram(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", tmp_path / "approvals.db")
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

    conn = webhook_server.sms_approval.init_db()
    try:
        pending = webhook_server.sms_approval.create_draft(
            conn,
            thread_key="prior-thread",
            customer_number="+14155550123",
            sender_number="+14155201316",
            draft_text="Old draft must stale when sensitive inbound arrives.",
        )
    finally:
        conn.close()

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Your OTP code is 773311 for login.",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    assert status["code"] == 200
    # A sensitive inbound (OTP/security code) is filtered: nothing forwarded to hooks,
    # no operator Telegram alert, and any pending draft is staled with the filtered
    # reason. The filter outcome lives in the captured side effects + approvals DB.
    assert hook_calls == []
    assert telegram_messages == []

    conn = webhook_server.sms_approval.init_db()
    try:
        stale_draft = webhook_server.sms_approval.get_draft(conn, pending["draft_id"])
    finally:
        conn.close()
    assert stale_draft["status"] == webhook_server.sms_approval.STATUS_STALE
    assert stale_draft["invalidated_reason"] == "new_inbound_filtered_sensitive"


def test_inbound_shortcode_sms_filtered_for_hook_and_telegram(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
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

    assert status["code"] == 200
    # A short-code sender (e.g. "12345") is filtered: never forwarded to hooks and no
    # operator Telegram alert. The filtered reason no longer rides the ACK response.
    assert hook_calls == []
    assert telegram_messages == []
    # Pin the centralized eligibility reason so a regression that reclassifies a
    # short-code sender (e.g. as filtered_sensitive) is caught, not just the
    # downstream hook/telegram suppression.
    decision = webhook_server.assess_inbound_sms_alert_eligibility(
        payload, from_number="12345", text="Code 009821 to verify."
    )
    assert decision["eligible"] is False
    assert decision["reason_code"] == "filtered_shortcode"


def test_inbound_hook_and_telegram_paths_share_eligible_result(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
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
        lambda text, **_kwargs: telegram_messages.append(text) or True,
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
    # An eligible inbound drives BOTH side effects from one shared decision: the hook
    # forwards exactly once and the operator gets exactly one Telegram alert.
    assert len(hook_calls) == 1
    assert len(telegram_messages) == 1


def test_inbound_sms_hook_respects_disabled_config(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: tmp_path / "dedupe.db")
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
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda _text, **_kwargs: True)

    # Wrap (not replace) the real hook sender so we observe the actual config-gated
    # result. With hooks disabled it must return (False, "disabled_by_config") rather
    # than performing any HTTP forward. The forward outcome no longer rides the ACK body.
    hook_results = []
    real_hook = webhook_server.send_sms_to_openclaw_hooks

    def _capturing_hook(normalized_sms, line_display=None):
        result = real_hook(normalized_sms, line_display=line_display)
        hook_results.append(result)
        return result

    monkeypatch.setattr(webhook_server, "send_sms_to_openclaw_hooks", _capturing_hook)

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Inbound hello",
    }
    handler, status = _build_handler(payload)
    webhook_server.DialpadWebhookHandler.handle_webhook(handler)

    assert status["code"] == 200
    assert hook_results == [(False, "disabled_by_config")]
