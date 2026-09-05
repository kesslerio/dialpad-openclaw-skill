"""Tests for inbound webhook delivery to Telegram and OpenClaw hooks."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import webhook_server
from inbound_driver import build_handler


def test_inbound_telegram_uses_enriched_sender(inbound_driver):
    inbound_driver.set_contact_lookup(contact_name="Jane Doe", status="resolved")
    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Inbound hello",
    }
    capture = inbound_driver.dispatch_sms(payload)

    assert capture.ack_code == 200
    assert len(capture.telegram_messages) == 1
    assert "From: Jane Doe (+14155550123)" in capture.telegram_messages[0]


def test_inbound_webhook_hook_uses_enriched_sender(inbound_driver):
    inbound_driver.set_contact_lookup(contact_name="Jane Doe", status="resolved")
    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Inbound hello",
    }
    capture = inbound_driver.dispatch_sms(payload)

    assert capture.ack_code == 200
    assert len(capture.hook_calls) == 1
    normalized_sms = capture.hook_calls[0]["normalized_sms"]
    assert normalized_sms["sender"] == "Jane Doe"
    assert normalized_sms["first_contact"]["knownContact"] is True
    assert normalized_sms["first_contact"]["keepBrief"] is True
    assert normalized_sms["first_contact"]["identityState"] == "resolved"
    assert normalized_sms["first_contact"]["lookup"]["status"] == "resolved"
    assert normalized_sms["first_contact"]["lookup"]["degraded"] is False


def test_inbound_sms_same_target_local_success_makes_hook_context_only(inbound_driver, monkeypatch):
    inbound_driver.set_contact_lookup(contact_name="Jane Doe", status="resolved")
    monkeypatch.setattr(webhook_server, "TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setattr(webhook_server, "TELEGRAM_CHAT_ID", "-5102073225")
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_CHANNEL", "telegram")
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_TO", "-5102073225")
    monkeypatch.setattr(webhook_server, "DIALPAD_ALLOW_DUPLICATE_OPERATOR_DELIVERY", False)

    hook_payloads = []
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: hook_payloads.append(
            webhook_server.build_openclaw_hook_payload(normalized_sms, line_display=line_display)
        ) or (True, "http_200"),
    )

    capture = inbound_driver.dispatch_sms({
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Inbound hello",
    })

    assert capture.ack_code == 200
    assert len(capture.telegram_messages) == 1
    assert hook_payloads[0]["deliver"] is False
    assert hook_payloads[0]["operatorNotification"]["hookDelivery"] == "context_only"


def test_inbound_sms_same_target_local_failure_keeps_hook_visible(inbound_driver, monkeypatch):
    inbound_driver.set_contact_lookup(contact_name="Jane Doe", status="resolved")
    monkeypatch.setattr(webhook_server, "TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setattr(webhook_server, "TELEGRAM_CHAT_ID", "-5102073225")
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_CHANNEL", "telegram")
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_TO", "-5102073225")
    monkeypatch.setattr(webhook_server, "DIALPAD_ALLOW_DUPLICATE_OPERATOR_DELIVERY", False)
    monkeypatch.setattr(webhook_server, "send_to_telegram", lambda *_args, **_kwargs: False)

    hook_payloads = []
    monkeypatch.setattr(
        webhook_server,
        "send_sms_to_openclaw_hooks",
        lambda normalized_sms, line_display=None: hook_payloads.append(
            webhook_server.build_openclaw_hook_payload(normalized_sms, line_display=line_display)
        ) or (True, "http_200"),
    )

    capture = inbound_driver.dispatch_sms({
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Inbound hello",
    })

    assert capture.ack_code == 200
    assert hook_payloads[0]["deliver"] is True
    assert hook_payloads[0]["operatorNotification"]["hookDelivery"] == "visible"


def test_inbound_webhook_hook_marks_unknown_sender_first_contact_candidate(inbound_driver):
    inbound_driver.set_contact_lookup(contact_name=None, status="not_found")
    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Who is this?",
    }
    capture = inbound_driver.dispatch_sms(payload)

    assert capture.ack_code == 200
    first_contact = capture.hook_calls[0]["normalized_sms"]["first_contact"]
    assert first_contact["knownContact"] is False
    assert first_contact["needsIdentityLookup"] is True
    assert first_contact["needsDraftReply"] is True
    assert first_contact["keepBrief"] is False


def test_inbound_telegram_escapes_markdown_content(inbound_driver):
    inbound_driver.set_contact_lookup(contact_name="Jane_Doe", status="resolved")
    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Need _bold_ *now* [check] `code`",
    }
    capture = inbound_driver.dispatch_sms(payload)

    assert capture.ack_code == 200
    assert len(capture.telegram_messages) == 1
    assert "Jane\\_Doe" in capture.telegram_messages[0]
    assert "Need \\_bold\\_ \\*now\\* \\[check] \\`code\\`" in capture.telegram_messages[0]


def test_inbound_sms_hook_respects_disabled_config(inbound_driver, monkeypatch):
    inbound_driver.set_contact_lookup(contact_name="Jane Doe", status="resolved")
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_SMS_ENABLED", False)
    monkeypatch.setattr(webhook_server, "OPENCLAW_HOOKS_TOKEN", "token-123")
    monkeypatch.setattr(webhook_server, "DIALPAD_SMS_TELEGRAM_NOTIFY", False)

    hook_results = []
    real_hook = inbound_driver.original_send_sms_to_openclaw_hooks

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
    capture = inbound_driver.dispatch_sms(payload)

    assert capture.ack_code == 200
    assert hook_results == [(False, "disabled_by_config")]


def test_pending_drafts_insert_and_claim(monkeypatch, tmp_path):
    """U1: insert a pending draft, claim it, second claim returns None."""
    db = tmp_path / "test_pending.db"
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: db)
    webhook_server.insert_pending_draft(
        "job-1", {"sender_number": "+15551234567"}, "fallback draft text", "token-abc",
        route_chat_id="-100", route_thread_id="3530", db_path=db,
    )
    claimed = webhook_server.claim_pending_draft("job-1", db_path=db)
    assert claimed is not None
    assert claimed["fallback_draft"] == "fallback draft text"
    assert claimed["route_chat_id"] == "-100"
    assert claimed["route_thread_id"] == "3530"
    # Second claim — already delivered
    claimed2 = webhook_server.claim_pending_draft("job-1", db_path=db)
    assert claimed2 is None


def test_pending_drafts_late_callback_after_timer(monkeypatch, tmp_path):
    """U1: timer claims first, callback gets None — no double-delivery."""
    db = tmp_path / "test_pending_late.db"
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: db)
    webhook_server.insert_pending_draft(
        "job-late", {"event_type": "sms"}, "fallback", "tok", db_path=db,
    )
    # Simulate timer winning
    timer_claim = webhook_server.claim_pending_draft("job-late", db_path=db)
    assert timer_claim is not None
    # Callback arrives late
    callback_claim = webhook_server.claim_pending_draft("job-late", db_path=db)
    assert callback_claim is None


def test_pending_drafts_callback_token_lookup(monkeypatch, tmp_path):
    """U1/U2: callback token lookup for auth verification."""
    db = tmp_path / "test_pending_token.db"
    monkeypatch.setattr(webhook_server, "_sms_dedupe_db_path", lambda: db)
    webhook_server.insert_pending_draft(
        "job-token", {}, "draft", "secret-token-xyz", db_path=db,
    )
    result = webhook_server.get_pending_draft_callback_token("job-token", db_path=db)
    assert result is not None
    assert result["token"] == "secret-token-xyz"
    assert result["status"] == "waiting"
    assert webhook_server.get_pending_draft_callback_token("nonexistent", db_path=db) is None


def test_hook_message_includes_callback_url_when_merged_flow_active():
    """U3: hook message includes callback URL + jobId when merged flow is active."""
    event = {
        "event_type": "sms",
        "sender": "John Doe",
        "sender_number": "+15551234567",
        "recipient_number": "+14155201316",
        "text": "I want to know cost please",
        "timestamp": 1782322817023,
    }
    msg = webhook_server.format_hook_message(
        event,
        callback_url="http://127.0.0.1:8081/internal/draft-callback",
        callback_job_id="job-123",
        callback_token="tok-abc",
    )
    assert "draft-callback" in msg
    assert "job-123" in msg
    assert "X-Callback-Token" in msg
    assert "tok-abc" in msg
    assert "Reply-Draft Callback" in msg


def test_hook_message_unchanged_when_no_callback():
    """U3: backward compat — no callback params → message unchanged."""
    event = {
        "event_type": "sms",
        "sender": "John Doe",
        "sender_number": "+15551234567",
        "recipient_number": "+14155201316",
        "text": "Hello",
        "timestamp": 123,
    }
    msg = webhook_server.format_hook_message(event)
    assert "Reply-Draft Callback" not in msg
    assert "draft-callback" not in msg


def test_hook_payload_deliver_false_when_merged_flow_active():
    """U3: deliver=False even if operator_notification would normally set it True."""
    event = {
        "event_type": "sms",
        "sender_number": "+15551234567",
        "recipient_number": "+14155201316",
        "text": "cost please",
        "operator_notification": {"deliver": True, "hookDelivery": "visible"},
        "callback_url": "http://127.0.0.1:8081/internal/draft-callback",
        "callback_job_id": "job-456",
        "callback_token": "tok",
    }
    payload = webhook_server.build_openclaw_hook_payload(event)
    assert payload["deliver"] is False
    assert "draft-callback" in payload["message"]
    assert "job-456" in payload["message"]


def test_hook_payload_deliver_respects_operator_notification_when_no_merge():
    """U3: backward compat — no callback params → deliver from operator_notification."""
    event = {
        "event_type": "sms",
        "sender_number": "+15551234567",
        "recipient_number": "+14155201316",
        "text": "hello",
        "operator_notification": {"deliver": True, "hookDelivery": "visible"},
    }
    payload = webhook_server.build_openclaw_hook_payload(event)
    assert payload["deliver"] is True
    assert "draft-callback" not in payload["message"]


def test_merged_flow_disabled_by_default(monkeypatch):
    """U5: DIALPAD_MERGED_DRAFT_FLOW defaults to False."""
    assert webhook_server.PORT == 8888
    assert webhook_server.DIALPAD_MERGED_DRAFT_FLOW is False
    assert webhook_server.DIALPAD_AGENT_DRAFT_TIMEOUT_SECONDS == 180


def test_generate_draft_job_id_unique():
    """U1: job IDs are unique."""
    ids = {webhook_server._generate_draft_job_id() for _ in range(100)}
    assert len(ids) == 100


def test_generate_callback_token_unique():
    """U1: callback tokens are unique."""
    tokens = {webhook_server._generate_callback_token() for _ in range(100)}
    assert len(tokens) == 100


def test_render_merged_card_sends_telegram(monkeypatch, tmp_path):
    """U4: _render_merged_card sends a Telegram card with the draft."""
    sent_messages = []
    monkeypatch.setattr(webhook_server, "send_to_telegram",
                        lambda text, **kw: sent_messages.append((text, kw)) or True)
    event = {
        "event_type": "sms",
        "sender_number": "+15551234567",
        "recipient_number": "+14155201316",
        "text": "cost please",
        "contact_name": "John",
        "line_display": "Sales",
        "inbound_context": {"identityConfidence": "high"},
        "auto_reply_draft_id": "draft-1",
        "fallback_draft": "fallback",
        "reply_policy": {"state": "eligible"},
    }
    claimed = {
        "event": event,
        "fallback_draft": "fallback",
        "route_chat_id": "-100",
        "route_thread_id": "3530",
    }
    webhook_server._render_merged_card("job-1", "agent draft text", claimed, path="callback", elapsed_ms=5000)
    assert len(sent_messages) == 1
    tg_text = sent_messages[0][0]
    assert "agent draft text" in tg_text
    assert sent_messages[0][1]["chat_id"] == "-100"
    assert sent_messages[0][1]["message_thread_id"] == "3530"


def test_hook_message_uses_submit_draft_tool_when_merged_flow_active():
    """U4: hook message references submit_draft tool, not raw HTTP POST."""
    event = {
        "event_type": "sms",
        "sender": "John",
        "sender_number": "+15551234567",
        "recipient_number": "+14155201316",
        "text": "hello",
        "timestamp": 123,
    }
    msg = webhook_server.format_hook_message(
        event,
        callback_url="http://127.0.0.1:8081/internal/draft-callback",
        callback_job_id="job-789",
        callback_token="tok-xyz",
    )
    assert "submit_draft" in msg
    assert "jobId" in msg
    assert "draft" in msg
    assert "draft-callback" in msg
