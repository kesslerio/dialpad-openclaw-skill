#!/usr/bin/env python3
"""Persistent approval gate for Dialpad SMS drafts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable


DB_PATH = Path(os.environ.get("DIALPAD_SMS_APPROVAL_DB", "/home/art/clawd/logs/sms_approvals.db"))
DEFAULT_EMERGENCY_OPT_OUT_PATH = Path("/tmp/dialpad_sms_approval_emergency_opt_outs.jsonl")

STATUS_PENDING = "pending"
STATUS_RISK_PENDING = "risk_pending"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_STALE = "stale"

RISK_NORMAL = "normal"
RISK_RISKY = "risky"

ACTION_APPROVE = "approve"
ACTION_CONFIRM_RISK = "confirm-risk"

BOT_ACTOR_IDS = {"", "agent", "bot", "openclaw", "niemand", "niemand-work"}
FAILED_DELIVERY_STATUSES = {
    "failed",
    "failure",
    "undelivered",
    "rejected",
    "error",
    "errored",
    "cancelled",
    "canceled",
}


def now_ms() -> int:
    return int(time.time() * 1000)


def normalize_phone_number(phone_number: str | None) -> str | None:
    if not phone_number:
        return None
    digits = "".join(ch for ch in str(phone_number) if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits or None


def emergency_opt_out_paths() -> list[Path]:
    configured = os.environ.get("DIALPAD_SMS_APPROVAL_EMERGENCY_PATH")
    if configured:
        return [Path(configured)]
    return [
        DB_PATH.with_name("sms_approval_emergency_opt_outs.jsonl"),
        DEFAULT_EMERGENCY_OPT_OUT_PATH,
    ]


def record_emergency_opt_out(
    *,
    customer_number: str,
    reason: str = "customer_opt_out",
    source: str | None = None,
    created_at_ms: int | None = None,
) -> Path:
    """Append a fail-closed opt-out marker outside the approval database."""
    normalized = normalize_phone_number(customer_number)
    if not normalized:
        raise ValueError("customer_number is required")

    payload = {
        "customer_number_normalized": normalized,
        "customer_number": customer_number,
        "reason": reason,
        "source": source,
        "created_at_ms": created_at_ms or now_ms(),
    }
    last_error: Exception | None = None
    for path in emergency_opt_out_paths():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
            return path
        except OSError as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise OSError("no emergency opt-out path configured")


def is_emergency_opted_out(customer_number: str | None) -> bool:
    normalized = normalize_phone_number(customer_number)
    if not normalized:
        return False

    for path in emergency_opt_out_paths():
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("customer_number_normalized") == normalized:
                        return True
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return False


def build_context_fingerprint(parts: dict[str, Any]) -> str:
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def init_db(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sms_approval_drafts (
            draft_id TEXT PRIMARY KEY,
            thread_key TEXT NOT NULL,
            source_inbound_id TEXT,
            customer_number TEXT NOT NULL,
            customer_number_normalized TEXT,
            sender_number TEXT NOT NULL,
            draft_text TEXT NOT NULL,
            risk_state TEXT NOT NULL,
            risk_reason TEXT,
            context_fingerprint TEXT,
            status TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            invalidated_at_ms INTEGER,
            invalidated_reason TEXT,
            first_confirmed_by TEXT,
            first_confirmed_username TEXT,
            first_confirmed_at_ms INTEGER,
            approved_by TEXT,
            approved_username TEXT,
            approved_at_ms INTEGER,
            dialpad_sms_id TEXT,
            delivery_status TEXT,
            send_error TEXT,
            metadata_json TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sms_approval_thread_status "
        "ON sms_approval_drafts(thread_key, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sms_approval_customer_status "
        "ON sms_approval_drafts(customer_number_normalized, status)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sms_approval_opt_outs (
            customer_number_normalized TEXT PRIMARY KEY,
            customer_number TEXT NOT NULL,
            reason TEXT NOT NULL,
            source TEXT,
            created_at_ms INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    if result.get("metadata_json"):
        try:
            result["metadata"] = json.loads(result["metadata_json"])
        except json.JSONDecodeError:
            result["metadata"] = None
    result.pop("metadata_json", None)
    return result


def create_draft(
    conn: sqlite3.Connection,
    *,
    thread_key: str,
    customer_number: str,
    sender_number: str,
    draft_text: str,
    source_inbound_id: str | None = None,
    risk_state: str = RISK_NORMAL,
    risk_reason: str | None = None,
    context_fingerprint: str | None = None,
    metadata: dict[str, Any] | None = None,
    draft_id: str | None = None,
    created_at_ms: int | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    if risk_state not in {RISK_NORMAL, RISK_RISKY}:
        raise ValueError(f"invalid risk_state: {risk_state}")
    text = draft_text.strip()
    if not text:
        raise ValueError("draft_text cannot be empty")
    if is_opted_out(conn, customer_number):
        raise ValueError("customer has opted out")

    resolved_draft_id = draft_id or f"smsdraft_{uuid.uuid4().hex[:16]}"
    conn.execute(
        """
        INSERT INTO sms_approval_drafts (
            draft_id, thread_key, source_inbound_id, customer_number,
            customer_number_normalized, sender_number, draft_text, risk_state,
            risk_reason, context_fingerprint, status, created_at_ms, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            resolved_draft_id,
            thread_key,
            source_inbound_id,
            customer_number,
            normalize_phone_number(customer_number),
            sender_number,
            text,
            risk_state,
            risk_reason,
            context_fingerprint,
            STATUS_PENDING,
            created_at_ms or now_ms(),
            json.dumps(metadata or {}, sort_keys=True),
        ),
    )
    if commit:
        conn.commit()
    return get_draft(conn, resolved_draft_id) or {}


def is_opted_out(conn: sqlite3.Connection, customer_number: str | None) -> bool:
    normalized = normalize_phone_number(customer_number)
    if not normalized:
        return False
    if is_emergency_opted_out(customer_number):
        return True
    row = conn.execute(
        "SELECT 1 FROM sms_approval_opt_outs WHERE customer_number_normalized = ?",
        (normalized,),
    ).fetchone()
    return row is not None


def mark_opt_out(
    conn: sqlite3.Connection,
    *,
    customer_number: str,
    reason: str = "customer_opt_out",
    source: str | None = None,
    created_at_ms: int | None = None,
) -> None:
    normalized = normalize_phone_number(customer_number)
    if not normalized:
        raise ValueError("customer_number is required")
    conn.execute(
        """
        INSERT INTO sms_approval_opt_outs (
            customer_number_normalized, customer_number, reason, source, created_at_ms
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(customer_number_normalized) DO UPDATE SET
            customer_number = excluded.customer_number,
            reason = excluded.reason,
            source = excluded.source,
            created_at_ms = excluded.created_at_ms
        """,
        (normalized, customer_number, reason, source, created_at_ms or now_ms()),
    )
    invalidate_pending(conn, customer_number=customer_number, reason=reason)


def get_draft(conn: sqlite3.Connection, draft_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM sms_approval_drafts WHERE draft_id = ?",
        (draft_id,),
    ).fetchone()
    return row_to_dict(row)


def invalidate_pending(
    conn: sqlite3.Connection,
    *,
    thread_key: str | None = None,
    customer_number: str | None = None,
    reason: str,
    invalidated_at_ms: int | None = None,
    exclude_draft_id: str | None = None,
    commit: bool = True,
) -> int:
    clauses = ["status IN (?, ?)"]
    params: list[Any] = [STATUS_PENDING, STATUS_RISK_PENDING]
    if thread_key:
        clauses.append("thread_key = ?")
        params.append(thread_key)
    elif customer_number:
        clauses.append("customer_number_normalized = ?")
        params.append(normalize_phone_number(customer_number))
    else:
        raise ValueError("thread_key or customer_number is required")
    if exclude_draft_id:
        clauses.append("draft_id != ?")
        params.append(exclude_draft_id)

    params = [STATUS_STALE, invalidated_at_ms or now_ms(), reason, *params]
    cursor = conn.execute(
        f"""
        UPDATE sms_approval_drafts
        SET status = ?, invalidated_at_ms = ?, invalidated_reason = ?
        WHERE {" AND ".join(clauses)}
        """,
        params,
    )
    if commit:
        conn.commit()
    return cursor.rowcount


def create_replacement_draft(
    conn: sqlite3.Connection,
    *,
    invalidate_thread_key: str | None = None,
    invalidate_customer_number: str | None = None,
    invalidated_reason: str = "superseded_by_new_draft",
    **draft_kwargs: Any,
) -> dict[str, Any]:
    """Atomically stale prior pending drafts and insert the replacement draft."""
    if not invalidate_thread_key and not invalidate_customer_number:
        raise ValueError("invalidate_thread_key or invalidate_customer_number is required")

    conn.execute("BEGIN IMMEDIATE")
    try:
        if invalidate_thread_key:
            invalidate_pending(
                conn,
                thread_key=invalidate_thread_key,
                reason=invalidated_reason,
                commit=False,
            )
        if invalidate_customer_number:
            invalidate_pending(
                conn,
                customer_number=invalidate_customer_number,
                reason=invalidated_reason,
                commit=False,
            )
        draft = create_draft(conn, commit=False, **draft_kwargs)
        conn.commit()
        return draft
    except Exception:
        conn.rollback()
        raise


def _is_bot_actor(actor_id: str | None, actor_is_bot: bool = False) -> bool:
    if actor_is_bot:
        return True
    normalized = str(actor_id or "").strip().lower()
    return normalized in BOT_ACTOR_IDS


def _actor_is_allowed(actor_id: str | None) -> bool:
    raw_allowlist = os.environ.get("DIALPAD_SMS_APPROVAL_ALLOWED_ACTORS", "")
    allowed = {item.strip() for item in raw_allowlist.split(",") if item.strip()}
    if not allowed:
        return True
    return str(actor_id or "").strip() in allowed


def _extract_send_result(result: Any) -> tuple[str | None, str | None]:
    if not isinstance(result, dict):
        return None, "unknown"
    sms_id = result.get("id") or result.get("message_id")
    status = result.get("delivery_status") or result.get("message_status") or result.get("status")
    return (str(sms_id) if sms_id is not None else None, str(status) if status is not None else None)


def _send_result_failure_reason(sms_id: str | None, delivery_status: str | None) -> str | None:
    if not sms_id:
        return "missing_dialpad_sms_id"
    normalized_status = str(delivery_status or "").strip().lower()
    if normalized_status in FAILED_DELIVERY_STATUSES:
        return f"delivery_status_{normalized_status}"
    return None


def approve_draft(
    conn: sqlite3.Connection,
    *,
    draft_id: str,
    actor_id: str,
    actor_username: str | None = None,
    action: str = ACTION_APPROVE,
    actor_is_bot: bool = False,
    send_func: Callable[..., Any] | None = None,
    approved_at_ms: int | None = None,
) -> dict[str, Any]:
    if _is_bot_actor(actor_id, actor_is_bot=actor_is_bot):
        return {"ok": False, "status": "blocked_actor", "sent": False, "reason": "agent_or_bot_cannot_approve"}
    if not _actor_is_allowed(actor_id):
        return {"ok": False, "status": "actor_not_allowed", "sent": False, "reason": "actor_not_in_allowlist"}
    if action not in {ACTION_APPROVE, ACTION_CONFIRM_RISK}:
        return {"ok": False, "status": "invalid_action", "sent": False, "reason": action}

    draft = get_draft(conn, draft_id)
    if not draft:
        return {"ok": False, "status": "not_found", "sent": False}
    if draft.get("status") == STATUS_SENT:
        return {"ok": True, "status": "already_resolved", "sent": False, "draft": draft}
    if draft.get("status") not in {STATUS_PENDING, STATUS_RISK_PENDING}:
        return {
            "ok": False,
            "status": "stale",
            "sent": False,
            "reason": draft.get("invalidated_reason") or draft.get("status"),
            "draft": draft,
        }
    if draft.get("invalidated_at_ms"):
        return {
            "ok": False,
            "status": "stale",
            "sent": False,
            "reason": draft.get("invalidated_reason") or "invalidated",
            "draft": draft,
        }
    if is_opted_out(conn, draft.get("customer_number")):
        return {
            "ok": False,
            "status": "blocked_opt_out",
            "sent": False,
            "reason": "customer_opt_out",
            "draft": draft,
        }

    ts = approved_at_ms or now_ms()
    if draft.get("risk_state") == RISK_RISKY:
        if action == ACTION_CONFIRM_RISK:
            if draft.get("status") != STATUS_RISK_PENDING or not draft.get("first_confirmed_at_ms"):
                return {
                    "ok": True,
                    "status": "risky_confirmation_required",
                    "sent": False,
                    "risk_reason": draft.get("risk_reason"),
                    "draft": draft,
                }
        else:
            cursor = conn.execute(
                """
                UPDATE sms_approval_drafts
                SET status = ?, first_confirmed_by = ?, first_confirmed_username = ?,
                    first_confirmed_at_ms = ?
                WHERE draft_id = ? AND status = ? AND first_confirmed_at_ms IS NULL
                """,
                (STATUS_RISK_PENDING, actor_id, actor_username, ts, draft_id, STATUS_PENDING),
            )
            conn.commit()
            return {
                "ok": True,
                "status": "risky_confirmation_required",
                "sent": False,
                "risk_reason": draft.get("risk_reason"),
                "draft": get_draft(conn, draft_id) if cursor.rowcount == 1 else draft,
            }

    expected_status = STATUS_RISK_PENDING if draft.get("risk_state") == RISK_RISKY else STATUS_PENDING
    cursor = conn.execute(
        """
        UPDATE sms_approval_drafts
        SET status = ?, approved_by = ?, approved_username = ?, approved_at_ms = ?,
            send_error = NULL
        WHERE draft_id = ?
          AND status = ?
          AND invalidated_at_ms IS NULL
        """,
        (STATUS_SENDING, actor_id, actor_username, ts, draft_id, expected_status),
    )
    conn.commit()
    if cursor.rowcount != 1:
        current = get_draft(conn, draft_id)
        return {
            "ok": False,
            "status": "stale",
            "sent": False,
            "reason": (current or {}).get("invalidated_reason") or (current or {}).get("status") or "not_claimed",
            "draft": current,
        }

    if send_func is None:
        from send_sms import send_sms as send_func

    try:
        result = send_func(
            [draft["customer_number"]],
            draft["draft_text"],
            from_number=draft["sender_number"],
        )
        sms_id, delivery_status = _extract_send_result(result)
        failure_reason = _send_result_failure_reason(sms_id, delivery_status)
        if failure_reason:
            conn.execute(
                """
                UPDATE sms_approval_drafts
                SET status = ?, dialpad_sms_id = ?, delivery_status = ?, send_error = ?
                WHERE draft_id = ? AND status = ?
                """,
                (STATUS_FAILED, sms_id, delivery_status, failure_reason, draft_id, STATUS_SENDING),
            )
            conn.commit()
            return {
                "ok": False,
                "status": STATUS_FAILED,
                "sent": False,
                "error": failure_reason,
                "dialpad_sms_id": sms_id,
                "delivery_status": delivery_status,
                "draft": get_draft(conn, draft_id),
            }
        conn.execute(
            """
            UPDATE sms_approval_drafts
            SET status = ?, dialpad_sms_id = ?, delivery_status = ?, send_error = NULL
            WHERE draft_id = ? AND status = ?
            """,
            (STATUS_SENT, sms_id, delivery_status, draft_id, STATUS_SENDING),
        )
        conn.commit()
        return {
            "ok": True,
            "status": STATUS_SENT,
            "sent": True,
            "dialpad_sms_id": sms_id,
            "delivery_status": delivery_status,
            "draft": get_draft(conn, draft_id),
        }
    except Exception as exc:  # noqa: BLE001 - persisted error boundary for external send.
        conn.execute(
            """
            UPDATE sms_approval_drafts
            SET status = ?, send_error = ?
            WHERE draft_id = ? AND status = ?
            """,
            (STATUS_FAILED, str(exc), draft_id, STATUS_SENDING),
        )
        conn.commit()
        return {
            "ok": False,
            "status": STATUS_FAILED,
            "sent": False,
            "error": str(exc),
            "draft": get_draft(conn, draft_id),
        }
