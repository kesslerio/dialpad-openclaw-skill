import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import webhook_server


def test_direct_pricing_classification_and_detection():
    direct_questions = [
        "How much is it",
        "How much is it?",
        "cost?",
        "cost",
        "price",
        "pricing",
        "pricing?",
        "how much does ShapeScale cost",
        "how much does it cost",
        "How much is ShapeScale?",
        "What is the price?",
        "what's the price",
        "what does it cost",
        "how much",
        "can you send pricing?",
    ]
    for q in direct_questions:
        assert webhook_server._is_direct_pricing_question(q) is True, f"Failed for {q}"
        assert webhook_server._is_pricing_question(q) is True, f"Failed for {q}"
        assert webhook_server.classify_rich_sms_question(q) == "pricing", f"Failed for {q}"


def test_direct_pricing_draft_uses_approved_lease_wording_exactly():
    normalized_event = {
        "event_type": "sms",
        "sender_number": "+13144494744",
        "recipient_number": "+14155201316",
        "text": "How much is it",
    }
    sender_enrichment = {
        "first_name": "Shirley",
        "contact_name": "Shirley Moore",
        "company": "Good Hydration Wellness Center & Spa",
    }

    rich_reply = webhook_server.build_rich_sms_reply(normalized_event, sender_enrichment=sender_enrichment)

    assert rich_reply["usable"] is True
    assert rich_reply["status"] == "ok"
    assert rich_reply["basis"] == "approved_pricing"
    assert rich_reply["category"] == "pricing"

    msg = rich_reply["message"]
    assert "Hi Shirley," in msg
    assert "$9,990" in msg
    assert "12-month lease with monthly billing: $499/month" in msg
    assert "12-month lease with annual billing: $5,388 upfront" in msg
    assert webhook_server.DIALPAD_BOOK_DEMO_URL in msg


def test_shirley_moore_scenario_suppresses_generic_crm_acknowledgement(monkeypatch, tmp_path):
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(webhook_server, "DIALPAD_AUTO_REPLY_SALES_LINE", "4155201316")
    monkeypatch.setattr(webhook_server, "lookup_recent_sms_thread", lambda *_a, **_k: [])
    monkeypatch.setattr(webhook_server.sms_approval, "DB_PATH", tmp_path / "approvals.db")

    crm_context = {
        "usable": True,
        "status": "ok",
        "basis": "attio",
        "company": "Good Hydration Wellness Center & Spa",
        "stage": "Demo Request",
        "summary": "Demo requested for Good Hydration Wellness Center & Spa",
    }
    monkeypatch.setattr(webhook_server, "lookup_sales_crm_context", lambda *_a, **_k: crm_context)

    normalized_event = {
        "event_type": "sms",
        "sender_number": "+13144494744",
        "recipient_number": "+14155201316",
        "text": "How much is it",
        "timestamp": 1750000000000,
        "first_contact": {
            "knownContact": True,
            "needsDraftReply": True,
            "lookup": {"status": "exact_match", "degraded": False},
        },
        "inbound_context": {
            "identityConfidence": "high",
            "contextDraftAllowed": True,
            "contactName": "Shirley Moore",
        },
        "crm_context": crm_context,
    }
    sender_enrichment = {
        "first_name": "Shirley",
        "contact_name": "Shirley Moore",
        "company": "Good Hydration Wellness Center & Spa",
    }

    assert webhook_server.should_send_proactive_reply(normalized_event, sender_enrichment=sender_enrichment) is True

    draft_message = webhook_server.build_proactive_reply_message(normalized_event, sender_enrichment=sender_enrichment)
    assert "thanks for the update" not in draft_message
    assert "will follow up shortly" not in draft_message
    assert "I have your ShapeScale demo conversation" not in draft_message
    assert "12-month lease with monthly billing: $499/month" in draft_message
    assert "12-month lease with annual billing: $5,388 upfront" in draft_message
    assert "$9,990" in draft_message

    created, status, msg, draft_id, policy = webhook_server.create_proactive_reply_draft(
        normalized_event,
        sender_enrichment=sender_enrichment,
    )
    assert created is True
    assert status == "draft_created"
    assert normalized_event["inbound_context"]["draftMode"] == "pricing_aware"
    assert normalized_event["inbound_context"]["richDraftAllowed"] is True
    assert normalized_event["inbound_context"]["richDraftBasis"] == "approved_pricing"


def test_build_contextual_sales_sms_reply_suppressed_for_pricing_intent():
    event = {
        "event_type": "sms",
        "sender_number": "+13144494744",
        "recipient_number": "+14155201316",
        "text": "How much is it",
        "inbound_context": {"contextDraftAllowed": True},
    }
    reply = webhook_server.build_contextual_sales_sms_reply(event)
    assert reply["usable"] is False
    assert reply["status"] == "pricing_intent"
    assert reply["category"] == "pricing"


def test_short_direct_pricing_variants():
    for text in ["cost?", "price", "how much does ShapeScale cost", "How much is it", "pricing"]:
        normalized_event = {
            "event_type": "sms",
            "sender_number": "+14155550123",
            "recipient_number": "+14155201316",
            "text": text,
        }
        reply = webhook_server.build_rich_sms_reply(normalized_event)
        assert reply["usable"] is True
        assert reply["status"] == "ok"
        assert reply["basis"] == "approved_pricing"
        assert "12-month lease with monthly billing: $499/month" in reply["message"]
        assert "12-month lease with annual billing: $5,388 upfront" in reply["message"]
        assert "$9,990" in reply["message"]
