"""Tests for inbound opt-out handling, STOP keyword, emergency blocks, and draft staling."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import webhook_server


def test_inbound_opt_out_blocks_hooks_sends_and_invalidates_pending_drafts(inbound_driver):
    inbound_driver.set_contact_lookup(
        contact_name="Lisa Primps (The Primping Place)",
        first_name="Lisa",
        last_name="Primps",
        company="The Primping Place",
        status="resolved",
    )

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
    capture = inbound_driver.dispatch_sms(payload)

    assert capture.ack_code == 200
    assert capture.hook_calls == []
    assert capture.sms_calls == []
    assert "human-only" in capture.telegram_messages[0]
    assert "Lisa Primps (The Primping Place) (+14155550123)" in capture.telegram_messages[0]
    assert "Message: I need a real person. Please don't bother me anymore." in capture.telegram_messages[0]

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
    assert pending_count == 0


def test_opt_out_persistence_failure_records_emergency_block(inbound_driver, monkeypatch, tmp_path):
    emergency_path = tmp_path / "emergency-opt-outs.jsonl"
    monkeypatch.setenv("DIALPAD_SMS_APPROVAL_EMERGENCY_PATH", str(emergency_path))

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
    capture = inbound_driver.dispatch_sms(payload)

    assert capture.ack_code == 200
    assert emergency_path.exists()
    assert capture.telegram_messages
    assert "persistence failed" not in capture.telegram_messages[0]
    assert "opt-out / human-only" in capture.telegram_messages[0]

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


def test_opt_out_persistence_total_failure_reports_failure_status(inbound_driver, monkeypatch, tmp_path):
    emergency_path = tmp_path / "emergency-opt-outs-dir"
    emergency_path.mkdir()
    monkeypatch.setenv("DIALPAD_SMS_APPROVAL_EMERGENCY_PATH", str(emergency_path))

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
    capture = inbound_driver.dispatch_sms(payload)

    assert capture.ack_code == 200
    assert "persistence failed" in capture.telegram_messages[0]

    conn = webhook_server.sms_approval.init_db()
    try:
        stale_draft = webhook_server.sms_approval.get_draft(conn, pending["draft_id"])
        opted_out = webhook_server.sms_approval.is_opted_out(conn, "+14155550123")
    finally:
        conn.close()
    assert stale_draft["status"] == webhook_server.sms_approval.STATUS_STALE
    assert opted_out is True


def test_standard_stop_keyword_blocks_sms_automation(inbound_driver):
    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "STOPALL",
    }
    capture = inbound_driver.dispatch_sms(payload)

    assert capture.ack_code == 200
    assert capture.hook_calls == []
    conn = webhook_server.sms_approval.init_db()
    try:
        opted_out = webhook_server.sms_approval.is_opted_out(conn, "+14155550123")
    finally:
        conn.close()
    assert opted_out is True


def test_opt_out_with_security_code_persists_opt_out_before_sensitive_filter(inbound_driver):
    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Your security code is 123456. Do not contact me.",
    }
    capture = inbound_driver.dispatch_sms(payload)

    conn = webhook_server.sms_approval.init_db()
    try:
        opted_out = webhook_server.sms_approval.is_opted_out(conn, "+14155550123")
    finally:
        conn.close()
    assert capture.ack_code == 200
    assert opted_out is True
    assert capture.hook_calls == []


def test_stop_by_phrase_does_not_create_permanent_opt_out(inbound_driver):
    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Can we stop by later?",
    }
    capture = inbound_driver.dispatch_sms(payload)

    conn = webhook_server.sms_approval.init_db()
    try:
        opted_out = webhook_server.sms_approval.is_opted_out(conn, "+14155550123")
    finally:
        conn.close()
    assert capture.ack_code == 200
    assert capture.hook_calls
    assert opted_out is False


def test_closed_office_autoresponder_with_boilerplate_does_not_opt_out(inbound_driver):
    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "Thank you for reaching out to ACME Clinic. Our office is closed until Monday 8am. Reply STOP to unsubscribe.",
    }
    capture = inbound_driver.dispatch_sms(payload)

    conn = webhook_server.sms_approval.init_db()
    try:
        opted_out = webhook_server.sms_approval.is_opted_out(conn, "+14155550123")
    finally:
        conn.close()
    assert capture.ack_code == 200
    assert capture.hook_calls
    assert opted_out is False


def test_second_inbound_without_conversation_id_invalidates_previous_draft(inbound_driver, monkeypatch):
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")

    first = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "message_id": "msg-1",
        "text": "First question.",
    }
    first_capture = inbound_driver.dispatch_sms(first)
    first_draft_id = first_capture.hook_calls[0]["normalized_sms"]["auto_reply"]["draftId"]
    assert first_draft_id

    second = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "message_id": "msg-2",
        "text": "Second question.",
    }
    second_capture = inbound_driver.dispatch_sms(second)

    conn = webhook_server.sms_approval.init_db()
    try:
        stale = webhook_server.sms_approval.get_draft(conn, first_draft_id)
    finally:
        conn.close()
    assert second_capture.ack_code == 200
    assert stale["status"] == webhook_server.sms_approval.STATUS_STALE
    assert stale["invalidated_reason"] == "superseded_by_new_draft"


def test_outbound_sms_invalidates_pending_approval_draft(inbound_driver):
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
    capture = inbound_driver.dispatch_sms(payload)

    conn = webhook_server.sms_approval.init_db()
    try:
        stale = webhook_server.sms_approval.get_draft(conn, draft["draft_id"])
        second_stale = webhook_server.sms_approval.get_draft(conn, second_draft["draft_id"])
    finally:
        conn.close()
    assert capture.ack_code == 200
    assert stale["status"] == webhook_server.sms_approval.STATUS_STALE
    assert stale["invalidated_reason"] == "manual_outbound"
    assert second_stale["status"] == webhook_server.sms_approval.STATUS_STALE
    assert second_stale["invalidated_reason"] == "manual_outbound"
    assert capture.hook_calls == []


def test_risky_inbound_sales_sms_creates_two_step_approval_draft(inbound_driver, monkeypatch):
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")

    payload = {
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": ["+14155201316"],
        "text": "I need to talk to a real person about the meeting time.",
    }
    capture = inbound_driver.dispatch_sms(payload)

    assert capture.ack_code == 200
    assert capture.sms_calls == []
    auto_reply = capture.hook_calls[0]["normalized_sms"]["auto_reply"]
    auto_reply_draft_id = auto_reply["draftId"]
    assert auto_reply["status"] == "draft_created"
    assert auto_reply_draft_id
    assert auto_reply["replyPolicy"]["state"] == "risky"
    assert "Second confirmation required" in capture.telegram_messages[0]
    assert "Risk:" in capture.telegram_messages[0]
    assert "--approval-token" in capture.telegram_messages[0]

    conn = webhook_server.sms_approval.init_db()
    try:
        draft = webhook_server.sms_approval.get_draft(conn, auto_reply_draft_id)
    finally:
        conn.close()
    assert draft["risk_state"] == webhook_server.sms_approval.RISK_RISKY
    assert draft["status"] == webhook_server.sms_approval.STATUS_PENDING


def test_previously_opted_out_customer_gets_blocked_status_not_persistence_failure(inbound_driver, monkeypatch):
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")

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
    capture = inbound_driver.dispatch_sms(payload)

    assert capture.ack_code == 200
    auto_reply = capture.hook_calls[0]["normalized_sms"]["auto_reply"]
    assert auto_reply["status"] == "blocked_opt_out"
    assert auto_reply["draftId"] is None
    assert auto_reply["replyPolicy"]["state"] == "blocked_opt_out"
    assert len(capture.telegram_messages) == 1
    assert "Automation blocked" in capture.telegram_messages[0]
    assert "human" in capture.telegram_messages[0]
    assert "No SMS approval draft" in capture.telegram_messages[0]
