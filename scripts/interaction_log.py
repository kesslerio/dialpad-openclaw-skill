#!/usr/bin/env python3
"""Canonical SMS + calls interaction-log facade.

The facade is the owner boundary for shared history.  It deliberately uses two
local SQLite files: messages stay in ``DIALPAD_SMS_DB`` and calls stay in the
existing ``DIALPAD_CALLS_DB``.  A remote host reaches this boundary through the
authenticated log API; it never opens either SQLite file.

Message identity prefers the Dialpad provider id.  When an observation has no
provider id, the fallback identity is a SHA-256 fingerprint over direction,
participants, the provider timestamp in a one-minute bucket, exact body (when
known), and MMS/body-shape fields.  The fallback is intentionally narrow and
is not used in place of a provider id when a provider id is available.

Merge rules are additive: known incoming values may fill or update metadata,
but an empty/unknown body can never replace a known body.  ``source`` is kept as
a pipe-separated provenance set so a webhook, local send, and later repair can
all be explained without changing the stable wrapper envelope.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import call_sqlite
import sms_sqlite


MESSAGE_FINGERPRINT_BUCKET_MS = 60 * 1000
CALL_FINGERPRINT_BUCKET_MS = 60 * 1000
MAX_LIMIT = 500
UNKNOWN_TOKENS = frozenset({"", "unknown", "n/a", "none", "null"})

MESSAGE_COLUMNS = (
    "dialpad_id",
    "contact_number",
    "contact_name",
    "direction",
    "from_number",
    "to_number",
    "text",
    "message_status",
    "delivery_result",
    "mms",
    "mms_url",
    "timestamp",
    "delivery_event_timestamp",
    "read",
    "interaction_key",
    "fingerprint",
    "fingerprint_base",
    "body_known",
    "body_source",
    "source",
    "observed_at",
)

CALL_COLUMNS = (
    "call_id",
    "provider_call_id",
    "entry_point_call_id",
    "interaction_key",
    "direction",
    "contact_number",
    "contact_name",
    "from_number",
    "to_number",
    "date_started",
    "date_ended",
    "duration",
    "call_state",
    "disposition",
    "line",
    "recording_url",
    "transcript_present",
    "transcript_text",
    "transcript_url",
    "raw_payload",
    "source",
    "observed_at",
    "reconciled_at",
)


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    value = str(value).strip()
    return value or None


def _known(value: Any) -> bool:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return False
    return str(value).strip().lower() not in UNKNOWN_TOKENS


def _known_body(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
    return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _observed_at(value: Any) -> tuple[str, bool]:
    if isinstance(value, str) and value.strip():
        return value.strip(), True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = sms_sqlite.parse_provider_event_timestamp(value)
        if parsed is not None:
            return _iso_from_ms(parsed) or _iso_now(), True
    return _iso_now(), False


def _phone_for_sms(value: Any) -> str | None:
    value = _first(value)
    if not _known(value):
        return None
    raw = str(value).strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if raw.startswith("+"):
        return f"+{digits}"
    return digits


def _phone_candidates(value: Any) -> list[str]:
    raw = _text(value)
    if not raw:
        return []
    digits = re.sub(r"\D", "", raw)
    values = [raw]
    if digits:
        values.append(digits)
        if len(digits) == 10:
            values.append(f"+1{digits}")
        elif len(digits) == 11 and digits.startswith("1"):
            values.append(f"+{digits}")
    if raw.startswith("+") and digits:
        values.append(f"+{digits}")
    return list(dict.fromkeys(values))


def _provider_id(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is None or isinstance(value, (bool, dict, list, tuple, set)):
            continue
        value = str(value).strip()
        if value:
            return value
    return None


def _source_set(existing: Any, incoming: Any) -> str | None:
    values: list[str] = []
    for value in (existing, incoming):
        if not _known(value):
            continue
        for item in str(value).split("|"):
            item = item.strip()
            if item and item not in values:
                values.append(item)
    return "|".join(values) if values else None


def _timestamp_ms(data: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key not in data:
            continue
        value = sms_sqlite.parse_provider_event_timestamp(data.get(key))
        if value is not None:
            return value
    return None


def _fingerprint(payload: dict[str, Any], *, bucket_ms: int, include_body: bool = True) -> str:
    timestamp = payload.get("timestamp")
    bucket = (int(timestamp) // bucket_ms) * bucket_ms if timestamp else None
    shape = {
        "direction": payload.get("direction"),
        "from_number": payload.get("from_number"),
        "to_number": payload.get("to_number"),
        "timestamp_bucket_ms": bucket,
        "body": payload.get("text") if include_body and payload.get("body_known") else None,
        "body_known": bool(payload.get("body_known")) if include_body else None,
        "body_length": len(payload.get("text") or "") if include_body and payload.get("body_known") else None,
        "mms": bool(payload.get("mms")),
        "mms_url": payload.get("mms_url") if payload.get("mms") else None,
    }
    encoded = json.dumps(shape, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _message_body(data: dict[str, Any]) -> tuple[str | None, bool]:
    if data.get("body_known") is False:
        return None, False
    candidate: str | None = None
    for key in ("body", "text", "text_content"):
        if key not in data:
            continue
        value = data.get(key)
        if not isinstance(value, str):
            continue
        if _known_body(value):
            return value, True
        candidate = value
    return candidate, False


def _contact_name(data: dict[str, Any], contact_number: str | None) -> str | None:
    contact = data.get("contact")
    values = []
    if isinstance(contact, dict):
        values.extend((contact.get("name"), contact.get("display_name")))
    values.extend((data.get("contact_name"), data.get("name")))
    for value in values:
        if _known(value) and str(value).strip() != str(contact_number or "").strip():
            return str(value).strip()
    return None


def normalize_message_observation(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize webhook, export-repair, and successful-send observations."""
    if not isinstance(data, dict):
        raise ValueError("message observation must be a JSON object")

    direction = str(data.get("direction") or "").strip().lower()
    if direction not in {"inbound", "outbound"}:
        raise ValueError("message observation direction must be inbound or outbound")

    from_number = _phone_for_sms(data.get("from_number", data.get("from")))
    to_number = _phone_for_sms(data.get("to_number", data.get("to")))
    contact_number = from_number if direction == "inbound" else to_number
    if not contact_number:
        raise ValueError("message observation requires a from/to phone number")

    body, body_known = _message_body(data)
    provider_id = _provider_id(data, ("provider_id", "message_id", "dialpad_id", "id"))
    timestamp = _timestamp_ms(
        data,
        ("timestamp", "created_date", "date_created", "message_timestamp", "event_timestamp"),
    )
    observed_at, observed_at_supplied = _observed_at(data.get("observed_at"))
    message_status = _first(data.get("message_status", data.get("status")))
    delivery_result = _first(data.get("delivery_result", data.get("message_delivery_result")))
    mms = _bool_value(data.get("mms"))
    if mms is None:
        mms = False
    mms_url = _text(data.get("mms_url"))
    source = _text(data.get("source")) or "webhook"
    normalized: dict[str, Any] = {
        "dialpad_id": provider_id,
        "contact_number": contact_number,
        "contact_name": _contact_name(data, contact_number),
        "direction": direction,
        "from_number": from_number,
        "to_number": to_number,
        "text": body,
        "message_status": str(message_status).strip() if _known(message_status) else None,
        "delivery_result": str(delivery_result).strip() if _known(delivery_result) else None,
        "mms": int(mms),
        "mms_url": mms_url,
        "timestamp": timestamp,
        "delivery_event_timestamp": _timestamp_ms(data, ("delivery_event_timestamp", "event_timestamp")),
        "read": _bool_value(data.get("read")) if "read" in data else None,
        "body_known": body_known,
        "body_source": source if body_known else None,
        "source": source,
        "observed_at": observed_at,
        "observed_at_supplied": observed_at_supplied,
    }
    normalized["fingerprint"] = _fingerprint(normalized, bucket_ms=MESSAGE_FINGERPRINT_BUCKET_MS)
    normalized["fingerprint_base"] = _fingerprint(
        normalized,
        bucket_ms=MESSAGE_FINGERPRINT_BUCKET_MS,
        include_body=False,
    )
    normalized["interaction_key"] = (
        f"provider:{provider_id}"
        if provider_id is not None
        else f"fingerprint:{normalized['fingerprint'] if body_known else normalized['fingerprint_base']}"
    )
    return normalized


def _message_find(conn, normalized: dict[str, Any]) -> Any:
    provider_id = normalized.get("dialpad_id")
    if provider_id is not None:
        row = conn.execute("SELECT * FROM messages WHERE dialpad_id = ?", (provider_id,)).fetchone()
        if row is not None:
            return row
        row = conn.execute(
            "SELECT * FROM messages WHERE interaction_key = ?",
            (f"provider:{provider_id}",),
        ).fetchone()
        if row is not None:
            return row
    row = conn.execute(
        "SELECT * FROM messages WHERE interaction_key = ?",
        (normalized["interaction_key"],),
    ).fetchone()
    if row is not None:
        return row
    # A provider id can upgrade a previously id-less row when the complete
    # observation has exactly the same constrained fallback fingerprint.
    if provider_id is not None:
        return conn.execute(
            "SELECT * FROM messages WHERE fingerprint = ? ORDER BY id LIMIT 1",
            (normalized["fingerprint"],),
        ).fetchone()
    row = conn.execute(
        "SELECT * FROM messages WHERE fingerprint_base = ? ORDER BY id LIMIT 1",
        (normalized["fingerprint_base"],),
    ).fetchone()
    if row is not None:
        return row
    return None


def _merge_message(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for field in (
        "contact_number",
        "contact_name",
        "direction",
        "from_number",
        "to_number",
        "message_status",
        "delivery_result",
        "mms_url",
    ):
        if _known(incoming.get(field)):
            merged[field] = incoming[field]
    if incoming.get("timestamp") is not None:
        merged["timestamp"] = incoming["timestamp"]
    if incoming.get("delivery_event_timestamp") is not None:
        merged["delivery_event_timestamp"] = incoming["delivery_event_timestamp"]
    if incoming.get("read") is not None and existing.get("read") is None:
        merged["read"] = int(bool(incoming["read"]))

    existing_body_known = bool(existing.get("body_known")) or _known_body(existing.get("text"))
    if incoming.get("body_known"):
        merged["text"] = incoming.get("text")
        merged["body_known"] = 1
        merged["body_source"] = _source_set(existing.get("body_source"), incoming.get("body_source"))
    else:
        merged["text"] = existing.get("text") if existing_body_known else incoming.get("text")
        merged["body_known"] = int(existing_body_known)
        merged["body_source"] = existing.get("body_source")

    if existing.get("mms") is None or incoming.get("mms"):
        merged["mms"] = int(bool(incoming.get("mms"))) if incoming.get("mms") is not None else existing.get("mms")
    merged["dialpad_id"] = incoming.get("dialpad_id") or existing.get("dialpad_id")
    merged["fingerprint"] = incoming.get("fingerprint") or existing.get("fingerprint")
    merged["fingerprint_base"] = incoming.get("fingerprint_base") or existing.get("fingerprint_base")
    if incoming.get("dialpad_id"):
        merged["interaction_key"] = incoming.get("interaction_key")
    elif incoming.get("body_known"):
        merged["interaction_key"] = f"fingerprint:{incoming.get('fingerprint')}"
    else:
        merged["interaction_key"] = existing.get("interaction_key") or incoming.get("interaction_key")
    merged["source"] = _source_set(existing.get("source"), incoming.get("source"))
    if incoming.get("observed_at_supplied"):
        merged["observed_at"] = incoming.get("observed_at")
    return merged


def _row_changed(existing: dict[str, Any], merged: dict[str, Any], columns: tuple[str, ...]) -> bool:
    return any(existing.get(column) != merged.get(column) for column in columns)


def _where_placeholders(values: list[str]) -> tuple[str, list[str]]:
    return ",".join("?" for _ in values), values


def _disposition(data: dict[str, Any], duration: int) -> str:
    if data.get("call_missed") is True or data.get("missed_call") is True or data.get("is_missed_call") is True:
        return "missed"
    raw = str(
        data.get("disposition")
        or data.get("disposition_name")
        or data.get("outcome")
        or data.get("call_state")
        or data.get("state")
        or ""
    ).strip().lower()
    if "voicemail" in raw:
        return "voicemail"
    if "cancel" in raw:
        return "canceled"
    if raw in {"missed", "no_answer", "unanswered", "no answer"} or "miss" in raw:
        return "missed"
    if raw in {"answered", "connected", "completed", "hangup", "ended", "answer"} or duration > 0:
        return "answered"
    return "unknown"


def _call_line(data: dict[str, Any]) -> str | None:
    for key in ("line", "line_number", "internal_number"):
        value = _text(data.get(key))
        if value:
            return value
    for parent_key in ("entry_point_target", "proxy_target", "target"):
        parent = data.get(parent_key)
        if isinstance(parent, dict):
            for key in ("name", "phone", "number"):
                value = _text(parent.get(key))
                if value:
                    return value
    return None


def normalize_call_observation(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("call observation must be a JSON object")
    direction = str(data.get("call_direction") or data.get("direction") or "unknown").strip().lower()
    if direction not in {"inbound", "outbound"}:
        direction = "unknown"
    contact = data.get("contact") if isinstance(data.get("contact"), dict) else {}
    from_number = _text(data.get("from_number") or data.get("caller_number") or contact.get("phone"))
    to_number = _text(data.get("to_number") or data.get("target_number"))
    if isinstance(data.get("to_number"), (list, tuple)):
        to_number = _text(_first(data.get("to_number")))
    contact_candidate = from_number if direction == "inbound" else to_number or from_number
    contact_number = call_sqlite.normalize_phone_number(contact_candidate)
    if not contact_number:
        contact_number = _phone_for_sms(contact_candidate)

    raw_duration = data.get("duration_seconds", data.get("duration", data.get("call_duration", 0)))
    if "duration_ms" in data:
        raw_duration = data.get("duration_ms")
        try:
            duration = max(0, int(float(raw_duration) / 1000))
        except (TypeError, ValueError):
            duration = 0
    else:
        try:
            duration = max(0, int(float(raw_duration or 0)))
        except (TypeError, ValueError):
            duration = 0

    provider_id = _provider_id(data, ("provider_call_id", "call_id", "id"))
    entry_point_call_id = _provider_id(data, ("entry_point_call_id", "entry_point_id"))
    timestamp = _timestamp_ms(data, ("date_started", "date_start", "start_time", "timestamp", "event_timestamp"))
    date_ended = _timestamp_ms(data, ("date_ended", "date_end", "end_time"))
    raw_state = _text(data.get("call_state") or data.get("state"))
    disposition = _disposition(data, duration)
    if raw_state:
        call_state = raw_state.lower()
    elif disposition != "unknown":
        call_state = "completed" if disposition == "answered" else disposition
    else:
        call_state = "unknown"
    transcript = call_sqlite.extract_transcript_text(data) or None
    recording_url = _text(
        data.get("recording_url")
        or data.get("recording_link")
        or data.get("call_review_share_link")
        or data.get("voicemail_link")
    )
    transcript_url = _text(data.get("transcript_url") or data.get("transcription_url") or data.get("transcript_review_url"))
    source = _text(data.get("source")) or "webhook"
    observed_at, observed_at_supplied = _observed_at(data.get("observed_at"))
    normalized: dict[str, Any] = {
        "provider_call_id": provider_id,
        "entry_point_call_id": entry_point_call_id,
        "direction": direction,
        "contact_number": contact_number,
        "contact_name": _contact_name(data, contact_number),
        "from_number": from_number,
        "to_number": to_number,
        "date_started": timestamp,
        "date_ended": date_ended,
        "duration": duration,
        "duration_known": "duration" in data or "call_duration" in data or "duration_seconds" in data or "duration_ms" in data,
        "call_state": call_state,
        "call_state_known": bool(raw_state),
        "disposition": disposition,
        "line": _call_line(data),
        "recording_url": recording_url,
        "transcript_present": int(bool(transcript)),
        "transcript_text": transcript,
        "transcript_url": transcript_url,
        "raw_payload": json.dumps(data, sort_keys=True, default=str, separators=(",", ":")),
        "source": source,
        "observed_at": observed_at,
        "observed_at_supplied": observed_at_supplied,
        "timestamp": timestamp,
    }
    normalized["fingerprint"] = _fingerprint(normalized, bucket_ms=CALL_FINGERPRINT_BUCKET_MS)
    normalized["interaction_key"] = (
        f"provider:{provider_id}" if provider_id is not None else f"fingerprint:{normalized['fingerprint']}"
    )
    normalized["call_id"] = provider_id or f"fingerprint:{normalized['fingerprint']}"
    return normalized


def _call_find(conn, normalized: dict[str, Any]) -> Any:
    provider_id = normalized.get("provider_call_id")
    if provider_id is not None:
        for query, value in (
            ("SELECT * FROM calls WHERE call_id = ?", provider_id),
            ("SELECT * FROM calls WHERE provider_call_id = ?", provider_id),
            ("SELECT * FROM calls WHERE interaction_key = ?", f"provider:{provider_id}"),
        ):
            row = conn.execute(query, (value,)).fetchone()
            if row is not None:
                return row
    row = conn.execute(
        "SELECT * FROM calls WHERE interaction_key = ?",
        (normalized["interaction_key"],),
    ).fetchone()
    if row is not None:
        return row
    if provider_id is not None:
        return conn.execute(
            "SELECT * FROM calls WHERE interaction_key = ?",
            (f"fingerprint:{normalized['fingerprint']}",),
        ).fetchone()
    return None


def _disposition_rank(value: Any) -> int:
    return {"unknown": 0, "missed": 1, "voicemail": 1, "canceled": 1, "answered": 2}.get(str(value or "unknown"), 0)


def _merge_call(existing: dict[str, Any], incoming: dict[str, Any], *, reconciler: bool) -> dict[str, Any]:
    merged = dict(existing)
    for field in (
        "direction",
        "contact_number",
        "contact_name",
        "from_number",
        "to_number",
        "date_started",
        "date_ended",
        "line",
        "recording_url",
        "transcript_url",
    ):
        if _known(incoming.get(field)) or (field in {"date_started", "date_ended"} and incoming.get(field) is not None):
            merged[field] = incoming[field]
    if incoming.get("duration_known") and int(incoming.get("duration") or 0) >= int(existing.get("duration") or 0):
        merged["duration"] = int(incoming.get("duration") or 0)
    if incoming.get("call_state_known") and _known(incoming.get("call_state")):
        merged["call_state"] = incoming["call_state"]
    elif not _known(existing.get("call_state")) and _known(incoming.get("call_state")):
        merged["call_state"] = incoming["call_state"]

    old_disposition = str(existing.get("disposition") or _disposition(existing, int(existing.get("duration") or 0)))
    new_disposition = str(incoming.get("disposition") or "unknown")
    if _disposition_rank(new_disposition) >= _disposition_rank(old_disposition) or old_disposition == "unknown":
        merged["disposition"] = new_disposition
    else:
        merged["disposition"] = old_disposition

    if incoming.get("transcript_present"):
        merged["transcript_present"] = 1
        merged["transcript_text"] = incoming.get("transcript_text")
    if incoming.get("raw_payload"):
        merged["raw_payload"] = incoming["raw_payload"]
    merged["provider_call_id"] = incoming.get("provider_call_id") or existing.get("provider_call_id")
    merged["entry_point_call_id"] = incoming.get("entry_point_call_id") or existing.get("entry_point_call_id")
    merged["call_id"] = incoming.get("provider_call_id") or existing.get("call_id") or incoming.get("call_id")
    merged["interaction_key"] = incoming.get("interaction_key") if incoming.get("provider_call_id") else existing.get("interaction_key")
    merged["source"] = _source_set(existing.get("source"), incoming.get("source"))
    if incoming.get("observed_at_supplied"):
        merged["observed_at"] = incoming.get("observed_at")
    if reconciler:
        merged["reconciled_at"] = _iso_now()
    return merged


class InteractionLog:
    """Read and write facade over the canonical SMS and calls SQLite files."""

    def __init__(self, sms_db: Path | str | None = None, calls_db: Path | str | None = None) -> None:
        default_sms = os.environ.get("DIALPAD_SMS_DB") or str(sms_sqlite.DB_PATH)
        default_calls = os.environ.get("DIALPAD_CALLS_DB") or str(call_sqlite.resolve_db_path())
        self.sms_db = Path(sms_db).expanduser() if sms_db is not None else Path(default_sms).expanduser()
        self.calls_db = Path(calls_db).expanduser() if calls_db is not None else Path(default_calls).expanduser()

    def record_message(self, observation: dict[str, Any], *, is_new: bool = True) -> dict[str, Any]:
        normalized = normalize_message_observation(observation)
        conn = sms_sqlite.init_db(self.sms_db)
        try:
            existing_row = _message_find(conn, normalized)
            old_phone = existing_row["contact_number"] if existing_row is not None else None
            if existing_row is None:
                values = {key: normalized.get(key) for key in MESSAGE_COLUMNS}
                if values["read"] is None:
                    values["read"] = 0 if is_new and normalized["direction"] == "inbound" else 1
                placeholders = ",".join("?" for _ in MESSAGE_COLUMNS)
                conn.execute(
                    f"INSERT INTO messages ({','.join(MESSAGE_COLUMNS)}) VALUES ({placeholders})",
                    tuple(values[key] for key in MESSAGE_COLUMNS),
                )
                created = True
                changed = True
            else:
                existing = dict(existing_row)
                merged = _merge_message(existing, normalized)
                values = {key: merged.get(key) for key in MESSAGE_COLUMNS}
                changed = _row_changed(existing, values, MESSAGE_COLUMNS)
                if changed:
                    assignments = ",".join(f"{key} = ?" for key in MESSAGE_COLUMNS)
                    conn.execute(
                        f"UPDATE messages SET {assignments} WHERE id = ?",
                        tuple(values[key] for key in MESSAGE_COLUMNS) + (existing["id"],),
                    )
                created = False
            phones = {value for value in (old_phone, normalized.get("contact_number")) if _known(value)}
            for phone in phones:
                sms_sqlite._update_contact_summary(conn, phone)
            conn.commit()
            row = conn.execute("SELECT * FROM messages WHERE interaction_key = ?", (values["interaction_key"],)).fetchone()
            if row is None and normalized.get("dialpad_id") is not None:
                row = conn.execute("SELECT * FROM messages WHERE dialpad_id = ?", (normalized["dialpad_id"],)).fetchone()
            message = dict(row) if row is not None else values
            message["body_known"] = bool(message.get("body_known")) or _known_body(message.get("text"))
            return {
                "status": "success",
                "stored": True,
                "created": created,
                "updated": changed and not created,
                "duplicate": not created and not changed,
                "identity_key": message.get("interaction_key"),
                "message": message,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_message_delivery(self, observation: dict[str, Any]) -> dict[str, Any]:
        conn = sms_sqlite.init_db(self.sms_db)
        try:
            return sms_sqlite.update_message_delivery(conn, observation)
        finally:
            conn.close()

    def thread(self, phone: str, limit: int = 100, cursor: Any = None) -> dict[str, Any]:
        del cursor  # Cursor pagination is intentionally deferred for v1.
        candidates = _phone_candidates(phone)
        if not candidates:
            raise ValueError("phone is required")
        limit = max(1, min(int(limit), MAX_LIMIT))
        conn = sms_sqlite.init_db(self.sms_db)
        try:
            placeholders, params = _where_placeholders(candidates)
            counts = conn.execute(
                f"""SELECT COUNT(*) AS count,
                    SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) AS outbound_count,
                    SUM(CASE WHEN direction = 'inbound' THEN 1 ELSE 0 END) AS inbound_count,
                    MAX(CASE WHEN direction = 'outbound' THEN timestamp END) AS latest_outbound_timestamp
                    FROM messages WHERE contact_number IN ({placeholders})""",
                params,
            ).fetchone()
            rows = conn.execute(
                f"SELECT * FROM messages WHERE contact_number IN ({placeholders}) ORDER BY timestamp DESC, id DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            messages = list(reversed(sms_sqlite.filter_messages([dict(row) for row in rows])))
            count = int(counts["count"] or 0)
            outbound_count = int(counts["outbound_count"] or 0)
            inbound_count = int(counts["inbound_count"] or 0)
            latest = counts["latest_outbound_timestamp"]
            return {
                "phone": phone,
                "count": count,
                "outbound_count": outbound_count,
                "inbound_count": inbound_count,
                "has_outbound": outbound_count > 0,
                "latest_outbound_timestamp": latest,
                "latest_outbound_timestamp_utc": _iso_from_ms(int(latest)) if latest else None,
                "messages": messages,
            }
        finally:
            conn.close()

    def inbox(self, limit: int = 100, cursor: Any = None, unread_only: bool = False) -> dict[str, Any]:
        del cursor
        limit = max(1, min(int(limit), MAX_LIMIT))
        conn = sms_sqlite.init_db(self.sms_db)
        try:
            where = "direction = 'inbound'"
            params: list[Any] = []
            if unread_only:
                where += " AND read = 0"
            count = int(conn.execute(f"SELECT COUNT(*) FROM messages WHERE {where}", params).fetchone()[0])
            rows = conn.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY timestamp DESC, id DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            return {"count": count, "unread_only": unread_only, "messages": sms_sqlite.filter_messages([dict(row) for row in rows])}
        finally:
            conn.close()

    def calls(
        self,
        hours: int | None = None,
        missed: bool = False,
        with_phone: str | None = None,
        limit: int = 100,
        cursor: Any = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> dict[str, Any]:
        del cursor
        if hours is not None and int(hours) <= 0:
            raise ValueError("hours must be greater than 0")
        limit = max(1, min(int(limit), MAX_LIMIT))
        if since_ms is None and hours is not None:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            since_ms = now_ms - int(hours) * 60 * 60 * 1000
        if until_ms is None:
            until_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        conn = call_sqlite.init_db(self.calls_db)
        try:
            clauses = ["1=1"]
            params: list[Any] = []
            if since_ms is not None:
                clauses.append("date_started >= ?")
                params.append(int(since_ms))
            if until_ms is not None:
                clauses.append("(date_started IS NULL OR date_started <= ?)")
                params.append(int(until_ms))
            if missed:
                clauses.append("(disposition = 'missed' OR (disposition IS NULL AND lower(call_state) IN ('missed', 'no_answer', 'unanswered')))" )
            if with_phone:
                digits = call_sqlite.normalize_phone_number(with_phone) or re.sub(r"\D", "", str(with_phone))
                if not digits:
                    raise ValueError("with must be a phone number")
                clauses.append("(contact_number = ? OR from_number LIKE ? OR to_number LIKE ?)")
                params.extend([digits, f"%{digits}%", f"%{digits}%"])
            where = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT * FROM calls WHERE {where} ORDER BY date_started DESC, id DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            summaries = [self._call_summary(dict(row)) for row in rows]
            return {"count": len(summaries), "calls": summaries}
        finally:
            conn.close()

    @staticmethod
    def _call_summary(call: dict[str, Any]) -> dict[str, Any]:
        duration = max(0, int(call.get("duration") or 0))
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        duration_display = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
        disposition = str(call.get("disposition") or _disposition(call, duration))
        direction = str(call.get("direction") or "unknown")
        contact_phone = call.get("contact_number") or call.get("from_number") or call.get("to_number")
        contact = call.get("contact_name") or contact_phone or "-"
        line = call.get("line")
        if not line:
            line = call.get("to_number") if direction == "inbound" else call.get("from_number")
        started_ms = call.get("date_started")
        return {
            "call_id": call.get("call_id"),
            "provider_call_id": call.get("provider_call_id"),
            "entry_point_call_id": call.get("entry_point_call_id"),
            "started_at": _iso_from_ms(int(started_ms)) if started_ms else None,
            "started_at_ms": started_ms,
            "contact": contact,
            "contact_phone": contact_phone,
            "direction": direction,
            "duration_seconds": duration,
            "duration_display": duration_display,
            "status": disposition,
            "state": call.get("call_state"),
            "disposition": disposition,
            "line": line,
            "recording_url": call.get("recording_url") or call.get("transcript_url"),
            "source": call.get("source") or "local_calls_db",
            "observed_at": call.get("observed_at"),
            "reconciled_at": call.get("reconciled_at"),
        }

    def record_call(
        self,
        observation: dict[str, Any],
        *,
        owner: bool = False,
        reconciler: bool = False,
    ) -> dict[str, Any]:
        """Record a call only from the owner webhook or a reconciler."""
        if not owner and not reconciler:
            raise PermissionError("call recording is restricted to the owner or reconciler")
        normalized = normalize_call_observation(observation)
        conn = call_sqlite.init_db(self.calls_db)
        try:
            existing_row = _call_find(conn, normalized)
            if existing_row is None:
                values = {key: normalized.get(key) for key in CALL_COLUMNS}
                values["reconciled_at"] = _iso_now() if reconciler else None
                placeholders = ",".join("?" for _ in CALL_COLUMNS)
                conn.execute(
                    f"INSERT INTO calls ({','.join(CALL_COLUMNS)}) VALUES ({placeholders})",
                    tuple(values[key] for key in CALL_COLUMNS),
                )
                created = True
                changed = True
            else:
                existing = dict(existing_row)
                merged = _merge_call(existing, normalized, reconciler=reconciler)
                values = {key: merged.get(key) for key in CALL_COLUMNS}
                changed = _row_changed(existing, values, CALL_COLUMNS)
                if changed:
                    assignments = ",".join(f"{key} = ?" for key in CALL_COLUMNS)
                    update_values = tuple(values[key] for key in CALL_COLUMNS) + (existing["id"],)
                    conn.execute(f"UPDATE calls SET {assignments} WHERE id = ?", update_values)
                created = False
            conn.commit()
            row = conn.execute("SELECT * FROM calls WHERE call_id = ?", (values["call_id"],)).fetchone()
            call = dict(row) if row is not None else values
            return {
                "status": "success",
                "stored": True,
                "created": created,
                "updated": changed and not created,
                "duplicate": not created and not changed,
                "identity_key": call.get("interaction_key"),
                "call": call,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def record_message(observation: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return InteractionLog(**kwargs).record_message(observation)


def record_call(observation: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return InteractionLog(**kwargs).record_call(observation, owner=True)
