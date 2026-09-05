"""Unit tests for call_sqlite storage manager."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import pytest

import call_sqlite


@pytest.fixture
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "calls.db"
        monkeypatch.setenv("DIALPAD_CALLS_DB", str(db_path))
        yield db_path


def test_init_db_creates_tables_and_indexes(temp_db):
    conn = call_sqlite.init_db(temp_db)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "calls" in tables

        indexes = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        ]
        assert "idx_calls_call_id" in indexes
        assert "idx_calls_contact_number" in indexes
        assert "idx_calls_date_started" in indexes
        assert "idx_calls_transcript" in indexes
    finally:
        conn.close()


def test_normalize_phone_number():
    assert call_sqlite.normalize_phone_number("+14155550123") == "4155550123"
    assert call_sqlite.normalize_phone_number("14155550123") == "4155550123"
    assert call_sqlite.normalize_phone_number("(415) 555-0123") == "4155550123"
    assert call_sqlite.normalize_phone_number("5550123") == "5550123"
    assert call_sqlite.normalize_phone_number("") is None
    assert call_sqlite.normalize_phone_number(None) is None


def test_parse_timestamp_ms():
    assert call_sqlite.parse_timestamp_ms(1700000000) == 1700000000000
    assert call_sqlite.parse_timestamp_ms(1700000000000) == 1700000000000
    assert call_sqlite.parse_timestamp_ms("1700000000") == 1700000000000
    assert call_sqlite.parse_timestamp_ms("2026-05-14T12:00:00Z") == 1778760000000
    assert call_sqlite.parse_timestamp_ms(None) is None
    assert call_sqlite.parse_timestamp_ms(False) is None
    assert call_sqlite.parse_timestamp_ms("invalid") is None


def test_extract_transcript_text():
    # String candidate
    payload_str = {"transcript": "Hello from caller."}
    assert call_sqlite.extract_transcript_text(payload_str) == "Hello from caller."

    # Utterances list with speakers
    payload_utterances = {
        "utterances": [
            {"speaker": "Agent", "text": "Hi there!"},
            {"speaker": "Customer", "text": "I need help with my scan."},
        ]
    }
    assert (
        call_sqlite.extract_transcript_text(payload_utterances)
        == "Agent: Hi there!\nCustomer: I need help with my scan."
    )

    # Lines list with transcript type
    payload_lines = {
        "lines": [
            {"type": "transcript", "text": "First line"},
            {"type": "action_item", "text": "Send email"},
            {"type": "transcript", "speaker": "Caller", "text": "Second line"},
        ]
    }
    assert call_sqlite.extract_transcript_text(payload_lines) == "First line\nCaller: Second line"

    # Empty payload
    assert call_sqlite.extract_transcript_text({}) == ""


def test_store_and_get_call(temp_db):
    call_data = {
        "call_id": "call-001",
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": "+14155201316",
        "contact_name": "Jane Doe",
        "date_started": 1700000000000,
        "date_ended": 1700000060000,
        "duration": 60,
        "call_state": "completed",
        "transcript": "Hello, I am calling regarding my order.",
        "transcript_url": "https://dialpad.com/review/call-001",
    }

    stored = call_sqlite.store_call(call_data, db_path=temp_db)
    assert stored["call_id"] == "call-001"
    assert stored["direction"] == "inbound"
    assert stored["contact_number"] == "4155550123"
    assert stored["contact_name"] == "Jane Doe"
    assert stored["duration"] == 60
    assert stored["call_state"] == "completed"
    assert stored["transcript_present"] is True
    assert stored["transcript_text"] == "Hello, I am calling regarding my order."
    assert stored["transcript_url"] == "https://dialpad.com/review/call-001"

    fetched = call_sqlite.get_call("call-001", db_path=temp_db)
    assert fetched is not None
    assert fetched["call_id"] == "call-001"
    assert fetched["transcript_present"] is True


def test_store_call_upsert_progression(temp_db):
    # Initial ringing / unanswered event
    initial = {
        "call_id": "call-002",
        "direction": "inbound",
        "from_number": "+14155550999",
        "to_number": "+14155201316",
        "date_started": 1700000100000,
        "call_state": "ringing",
        "duration": 0,
    }
    call_sqlite.store_call(initial, db_path=temp_db)

    first_record = call_sqlite.get_call("call-002", db_path=temp_db)
    assert first_record["call_state"] == "ringing"
    assert first_record["duration"] == 0
    assert first_record["transcript_present"] is False

    # Second event when call completed with duration and transcript
    completed = {
        "call_id": "call-002",
        "direction": "inbound",
        "from_number": "+14155550999",
        "to_number": "+14155201316",
        "contact_name": "Bob Smith",
        "date_started": 1700000100000,
        "date_ended": 1700000220000,
        "call_state": "completed",
        "duration": 120,
        "transcript": "Thank you for answering.",
    }
    call_sqlite.store_call(completed, db_path=temp_db)

    updated_record = call_sqlite.get_call("call-002", db_path=temp_db)
    assert updated_record["call_state"] == "completed"
    assert updated_record["duration"] == 120
    assert updated_record["contact_name"] == "Bob Smith"
    assert updated_record["transcript_present"] is True
    assert updated_record["transcript_text"] == "Thank you for answering."


def test_get_call_transcript_record(temp_db):
    call_with_transcript = {
        "call_id": "call-trans-1",
        "direction": "inbound",
        "from_number": "+14155551111",
        "to_number": "+14155201316",
        "date_started": 1700000500000,
        "duration": 90,
        "transcript": "Detailed discussion about pricing.",
        "transcript_url": "https://dialpad.com/review/call-trans-1",
    }
    call_sqlite.store_call(call_with_transcript, db_path=temp_db)

    rec = call_sqlite.get_call_transcript_record("call-trans-1", db_path=temp_db)
    assert rec["available"] is True
    assert rec["status"] == "available"
    assert rec["transcript_text"] == "Detailed discussion about pricing."
    assert rec["transcript_review_url"] == "https://dialpad.com/review/call-trans-1"
    assert rec["source"] == "local_calls_db"
    assert rec["unavailable_reason"] is None
    assert rec["call"]["id"] == "call-trans-1"

    # Non-existent call
    missing = call_sqlite.get_call_transcript_record("call-nonexistent", db_path=temp_db)
    assert missing["available"] is False
    assert missing["status"] == "unavailable"
    assert missing["unavailable_reason"] == "call_not_found"

    # Call without transcript
    call_no_transcript = {
        "call_id": "call-notrans-1",
        "direction": "outbound",
        "from_number": "+14155201316",
        "to_number": "+14155552222",
        "date_started": 1700000600000,
        "duration": 15,
    }
    call_sqlite.store_call(call_no_transcript, db_path=temp_db)
    no_trans_rec = call_sqlite.get_call_transcript_record("call-notrans-1", db_path=temp_db)
    assert no_trans_rec["available"] is False
    assert no_trans_rec["unavailable_reason"] == "no_transcript"


def test_list_stored_calls_filtering(temp_db):
    # Insert several calls
    calls = [
        {
            "call_id": f"c-{i}",
            "direction": "inbound" if i % 2 == 0 else "outbound",
            "from_number": "+14155550100" if i < 3 else "+14155550200",
            "to_number": "+14155201316",
            "duration": i * 30,
            "date_started": 1700000000000 + i * 100000,
            "transcript": "Transcript text" if i >= 2 else None,
        }
        for i in range(5)
    ]
    for c in calls:
        call_sqlite.store_call(c, db_path=temp_db)

    # Filter by phone
    phone_matches = call_sqlite.list_stored_calls(phone="+14155550100", db_path=temp_db)
    assert len(phone_matches) == 3

    # Filter by direction
    inbound_matches = call_sqlite.list_stored_calls(direction="inbound", db_path=temp_db)
    assert all(c["direction"] == "inbound" for c in inbound_matches)

    # Filter by min_duration
    duration_matches = call_sqlite.list_stored_calls(min_duration=60, db_path=temp_db)
    assert all(c["duration"] >= 60 for c in duration_matches)
    assert len(duration_matches) == 3  # i=2 (60), i=3 (90), i=4 (120)

    # Filter by transcript_only
    trans_matches = call_sqlite.list_stored_calls(transcript_only=True, db_path=temp_db)
    assert len(trans_matches) == 3
    assert all(c["transcript_present"] is True for c in trans_matches)

    # Limit
    limited = call_sqlite.list_stored_calls(limit=2, db_path=temp_db)
    assert len(limited) == 2
