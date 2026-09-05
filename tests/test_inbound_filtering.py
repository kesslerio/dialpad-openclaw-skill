"""Tests for sensitive SMS and shortcode inbound filtering."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import webhook_server


def test_inbound_sensitive_sms_filtered_for_hook_and_telegram(inbound_driver):
    inbound_driver.set_contact_lookup(contact_name="Capital One", status="resolved")

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
    capture = inbound_driver.dispatch_sms(payload)

    assert capture.ack_code == 200
    assert capture.hook_calls == []
    assert capture.telegram_messages == []

    conn = webhook_server.sms_approval.init_db()
    try:
        stale_draft = webhook_server.sms_approval.get_draft(conn, pending["draft_id"])
    finally:
        conn.close()
    assert stale_draft["status"] == webhook_server.sms_approval.STATUS_STALE
    assert stale_draft["invalidated_reason"] == "new_inbound_filtered_sensitive"


def test_inbound_shortcode_sms_filtered_for_hook_and_telegram(inbound_driver):
    inbound_driver.set_contact_lookup(contact_name="Unknown", status="not_found")
    payload = {
        "direction": "inbound",
        "from_number": "12345",
        "to_number": ["+14155201316"],
        "text": "Code 009821 to verify.",
    }
    capture = inbound_driver.dispatch_sms(payload)

    assert capture.ack_code == 200
    assert capture.hook_calls == []
    assert capture.telegram_messages == []

    decision = webhook_server.assess_inbound_sms_alert_eligibility(
        payload, from_number="12345", text="Code 009821 to verify."
    )
    assert decision["eligible"] is False
    assert decision["reason_code"] == "filtered_shortcode"


def test_inbound_hook_and_telegram_paths_share_eligible_result(inbound_driver):
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
    assert len(capture.telegram_messages) == 1
