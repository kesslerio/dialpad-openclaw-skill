#!/usr/bin/env python3
"""Append authoritative Dialpad SMS send receipts to a JSONL ledger."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


DEFAULT_LEDGER_PATH = Path("/data/.openclaw/state/dialpad/sms-receipts.jsonl")
MAX_LEDGER_BYTES = 5 * 1024 * 1024
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

AppendReceiptStatus = Literal["appended", "not_applicable", "append_failed"]


def _ledger_path() -> Path:
    return Path(os.environ.get("DIALPAD_SMS_RECEIPT_LEDGER", str(DEFAULT_LEDGER_PATH)))


def extract_send_result(result: Any) -> tuple[str | None, str | None]:
    if not isinstance(result, dict):
        return None, "unknown"
    sms_id = result.get("id") or result.get("message_id")
    status = result.get("delivery_status") or result.get("message_status") or result.get("status")
    return (str(sms_id) if sms_id is not None else None, str(status) if status is not None else None)


def send_result_failure_reason(sms_id: str | None, delivery_status: str | None) -> str | None:
    if not sms_id:
        return "missing_dialpad_sms_id"
    normalized_status = str(delivery_status or "").strip().lower()
    if normalized_status in FAILED_DELIVERY_STATUSES:
        return f"delivery_status_{normalized_status}"
    return None


def _request_recipients(request_payload: dict[str, Any]) -> list[str]:
    raw_to = request_payload.get("to_numbers")
    if raw_to is None:
        raw_to = request_payload.get("to")
    if isinstance(raw_to, str):
        raw_values = [raw_to]
    elif isinstance(raw_to, list):
        raw_values = raw_to
    else:
        raw_values = []
    return [str(value) for value in raw_values if str(value).strip()]


def _rotate_if_needed(path: Path) -> None:
    if not path.is_file():
        return
    if path.stat().st_size <= MAX_LEDGER_BYTES:
        return
    rotated = path.with_name(f"{path.name}.1")
    if rotated.exists():
        rotated.unlink()
    path.rename(rotated)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    _rotate_if_needed(path)

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fd = -1
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    finally:
        if fd >= 0:
            os.close(fd)


def append_receipt(
    *,
    request_payload: dict[str, Any],
    send_result: Any,
    source: str,
) -> AppendReceiptStatus:
    """Append a receipt for a successful send.

    Returns ``append_failed`` for non-fatal ledger write failures, ``appended``
    for a written receipt, and ``not_applicable`` when the send result is not a
    successful Dialpad SMS response. Never raises.
    """
    try:
        sms_id, delivery_status = extract_send_result(send_result)
        failure_reason = send_result_failure_reason(sms_id, delivery_status)
        if failure_reason:
            return "not_applicable"

        recipients = _request_recipients(request_payload)
        if not recipients:
            return "not_applicable"

        payload = {
            "schema_version": "1",
            "message_id": sms_id,
            "to": recipients,
            "from": request_payload.get("from_number"),
            "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "delivery_status": delivery_status or "unknown",
            "source": source,
        }

        _append_jsonl(_ledger_path(), payload)
    except Exception as exc:  # noqa: BLE001 - receipt persistence is observational only.
        print(f"Warning: failed to append Dialpad SMS receipt ledger: {exc}", file=sys.stderr)
        return "append_failed"

    return "appended"
