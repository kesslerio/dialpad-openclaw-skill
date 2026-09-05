#!/usr/bin/env python3
"""
Call SQLite Storage Manager for Dialpad.
Stores call metadata and transcripts in a local append-only SQLite database.
Mirroring DIALPAD_SMS_DB for calls.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def resolve_db_path() -> Path:
    """Resolve the SQLite database path from DIALPAD_CALLS_DB or default."""
    return Path(os.environ.get("DIALPAD_CALLS_DB", "/home/art/niemand/logs/calls.db")).expanduser()


def normalize_phone_number(phone_number: Any) -> Optional[str]:
    """Normalize phone number to digits (last 10 digits for standard US numbers)."""
    if not phone_number:
        return None
    digits = "".join(ch for ch in str(phone_number) if ch.isdigit())
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def parse_timestamp_ms(value: Any) -> Optional[int]:
    """Normalize timestamps (seconds, milliseconds, ISO string) to integer milliseconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        val_float = float(value)
        # If in seconds (e.g. < 2000000000), convert to milliseconds
        if val_float < 10000000000:
            return int(val_float * 1000)
        return int(val_float)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            val_int = int(text)
            if val_int < 10000000000:
                return val_int * 1000
            return val_int
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            return None
    return None


def extract_transcript_text(payload: dict[str, Any]) -> str:
    """Extract and format transcript text from a payload dictionary."""
    string_candidates = (
        payload.get("transcript"),
        payload.get("transcription_text"),
        payload.get("transcript_text"),
        payload.get("text"),
        payload.get("full_text"),
        payload.get("content"),
    )
    for candidate in string_candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    list_candidate_key = None
    list_candidates = []
    for key in ("utterances", "segments", "items", "lines", "transcript"):
        value = payload.get(key)
        if isinstance(value, list):
            list_candidate_key = key
            list_candidates = value
            break

    lines: list[str] = []
    for item in list_candidates:
        if not isinstance(item, dict):
            continue
        if list_candidate_key == "lines" and item.get("type") and item.get("type") != "transcript":
            continue

        text = (
            item.get("text")
            or item.get("transcript")
            or item.get("content")
            or item.get("utterance")
        )
        if not isinstance(text, str) or not text.strip():
            continue
        speaker = (
            item.get("speaker")
            or item.get("speaker_name")
            or item.get("name")
            or item.get("participant")
            or item.get("role")
        )
        if isinstance(speaker, str) and speaker.strip():
            lines.append(f"{speaker.strip()}: {text.strip()}")
        else:
            lines.append(text.strip())

    return "\n".join(lines).strip()


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Initialize the calls database and create tables/indexes."""
    target_path = Path(db_path).expanduser() if db_path else resolve_db_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target_path)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY,
            call_id TEXT UNIQUE NOT NULL,
            direction TEXT,
            contact_number TEXT,
            contact_name TEXT,
            from_number TEXT,
            to_number TEXT,
            date_started INTEGER,
            date_ended INTEGER,
            duration INTEGER DEFAULT 0,
            call_state TEXT,
            transcript_present BOOLEAN DEFAULT 0,
            transcript_text TEXT,
            transcript_url TEXT,
            raw_payload TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_call_id ON calls(call_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_contact_number ON calls(contact_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_date_started ON calls(date_started)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_transcript ON calls(transcript_present)")
    conn.commit()
    return conn


def store_call(
    call_data: dict[str, Any],
    transcript_text: str | None = None,
    transcript_url: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Store or update a call event record with metadata and optional transcript."""
    call_id = str(call_data.get("call_id") or call_data.get("id") or "").strip()
    if not call_id:
        call_id = str(call_data.get("entry_point_call_id") or "").strip()
    if not call_id:
        raise ValueError("Call data missing required call_id or id")

    direction = str(call_data.get("call_direction") or call_data.get("direction") or "unknown").strip().lower()
    if direction not in {"inbound", "outbound"}:
        direction = "unknown"

    from_number = call_data.get("from_number")
    if not from_number and isinstance(call_data.get("contact"), dict):
        from_number = call_data["contact"].get("phone")
    if from_number:
        from_number = str(from_number).strip()

    to_number = call_data.get("to_number")
    if not to_number:
        to_number = call_data.get("target_number")
    if to_number:
        to_number = str(to_number).strip()

    contact_number = from_number if direction == "inbound" else (to_number or from_number)
    contact_number_normalized = normalize_phone_number(contact_number)

    contact_name = call_data.get("contact_name")
    if not contact_name and isinstance(call_data.get("contact"), dict):
        contact_name = call_data["contact"].get("name")

    date_started = parse_timestamp_ms(
        call_data.get("date_started")
        or call_data.get("start_time")
        or call_data.get("date_start")
        or call_data.get("timestamp")
    )
    date_ended = parse_timestamp_ms(
        call_data.get("date_ended") or call_data.get("end_time") or call_data.get("date_end")
    )

    raw_duration = call_data.get("duration") or call_data.get("call_duration") or 0
    try:
        duration = max(0, int(float(raw_duration)))
    except (TypeError, ValueError):
        duration = 0

    call_missed = bool(call_data.get("call_missed", False))
    raw_state = str(call_data.get("call_state") or "").strip().lower()
    if call_missed:
        call_state = "missed"
    elif raw_state:
        call_state = raw_state
    else:
        call_state = "missed" if (direction == "inbound" and duration == 0) else "completed"

    extracted_transcript = transcript_text or extract_transcript_text(call_data) or None
    resolved_transcript_url = (
        transcript_url
        or call_data.get("transcript_url")
        or call_data.get("transcription_url")
        or call_data.get("transcript_review_url")
    )
    transcript_present = 1 if (extracted_transcript and extracted_transcript.strip()) else 0

    raw_payload_str = None
    try:
        raw_payload_str = json.dumps(call_data)
    except Exception:
        pass

    conn = init_db(db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO calls (
                    call_id, direction, contact_number, contact_name, from_number, to_number,
                    date_started, date_ended, duration, call_state, transcript_present,
                    transcript_text, transcript_url, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(call_id) DO UPDATE SET
                    direction = CASE WHEN excluded.direction != 'unknown' THEN excluded.direction ELSE calls.direction END,
                    contact_number = COALESCE(excluded.contact_number, calls.contact_number),
                    contact_name = COALESCE(excluded.contact_name, calls.contact_name),
                    from_number = COALESCE(excluded.from_number, calls.from_number),
                    to_number = COALESCE(excluded.to_number, calls.to_number),
                    date_started = COALESCE(excluded.date_started, calls.date_started),
                    date_ended = COALESCE(excluded.date_ended, calls.date_ended),
                    duration = CASE WHEN excluded.duration > 0 THEN excluded.duration ELSE calls.duration END,
                    call_state = CASE WHEN excluded.call_state != 'unknown' THEN excluded.call_state ELSE calls.call_state END,
                    transcript_present = CASE WHEN excluded.transcript_present = 1 THEN 1 ELSE calls.transcript_present END,
                    transcript_text = COALESCE(excluded.transcript_text, calls.transcript_text),
                    transcript_url = COALESCE(excluded.transcript_url, calls.transcript_url),
                    raw_payload = COALESCE(excluded.raw_payload, calls.raw_payload),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    call_id,
                    direction,
                    contact_number_normalized or contact_number,
                    contact_name,
                    from_number,
                    to_number,
                    date_started,
                    date_ended,
                    duration,
                    call_state,
                    transcript_present,
                    extracted_transcript,
                    resolved_transcript_url,
                    raw_payload_str,
                ),
            )
        return get_call(call_id, db_path=db_path) or {}
    finally:
        conn.close()


def get_call(call_id: str, db_path: Path | str | None = None) -> Optional[Dict[str, Any]]:
    """Retrieve a call record and transcript by call_id."""
    if not call_id:
        return None
    conn = init_db(db_path)
    try:
        row = conn.execute("SELECT * FROM calls WHERE call_id = ?", (str(call_id).strip(),)).fetchone()
        if not row:
            return None
        res = dict(row)
        res["transcript_present"] = bool(res.get("transcript_present"))
        return res
    finally:
        conn.close()


def list_stored_calls(
    phone: str | None = None,
    direction: str | None = None,
    min_duration: int | None = None,
    transcript_only: bool = False,
    since: int | None = None,
    limit: int = 50,
    db_path: Path | str | None = None,
) -> List[Dict[str, Any]]:
    """Query stored calls with deterministic filtering."""
    query = "SELECT * FROM calls WHERE 1=1"
    params: list[Any] = []

    if phone:
        normalized = normalize_phone_number(phone)
        if normalized:
            query += " AND (contact_number = ? OR from_number LIKE ? OR to_number LIKE ?)"
            params.extend([normalized, f"%{normalized}%", f"%{normalized}%"])
        else:
            query += " AND (contact_number = ? OR from_number = ? OR to_number = ?)"
            params.extend([phone, phone, phone])

    if direction and direction in {"inbound", "outbound"}:
        query += " AND direction = ?"
        params.append(direction)

    if min_duration is not None and min_duration > 0:
        query += " AND duration >= ?"
        params.append(int(min_duration))

    if transcript_only:
        query += " AND transcript_present = 1"

    if since is not None and since > 0:
        query += " AND date_started >= ?"
        params.append(int(since))

    query += " ORDER BY date_started DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))

    conn = init_db(db_path)
    try:
        rows = conn.execute(query, params).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            d["transcript_present"] = bool(d.get("transcript_present"))
            results.append(d)
        return results
    finally:
        conn.close()


def get_call_transcript_record(call_id: str, db_path: Path | str | None = None) -> Dict[str, Any]:
    """Retrieve stored call transcript matching the format expected by get_call_transcript wrapper."""
    call = get_call(call_id, db_path=db_path)
    if not call or not call.get("transcript_present") or not call.get("transcript_text"):
        return {
            "call_id": call_id,
            "available": False,
            "status": "unavailable",
            "source": "local_calls_db",
            "unavailable_reason": "call_not_found" if not call else "no_transcript",
            "transcript_text": None,
            "transcript_review_url": call.get("transcript_url") if call else None,
            "call": {
                "id": call.get("call_id"),
                "direction": call.get("direction"),
                "contact_number": call.get("contact_number"),
                "contact_name": call.get("contact_name"),
                "date_started": call.get("date_started"),
                "duration": call.get("duration"),
                "call_state": call.get("call_state"),
            } if call else None,
        }

    return {
        "call_id": call_id,
        "available": True,
        "status": "available",
        "transcript_text": call["transcript_text"],
        "transcript_review_url": call.get("transcript_url"),
        "source": "local_calls_db",
        "unavailable_reason": None,
        "call": {
            "id": call.get("call_id"),
            "direction": call.get("direction"),
            "contact_number": call.get("contact_number"),
            "contact_name": call.get("contact_name"),
            "date_started": call.get("date_started"),
            "duration": call.get("duration"),
            "call_state": call.get("call_state"),
        },
    }
