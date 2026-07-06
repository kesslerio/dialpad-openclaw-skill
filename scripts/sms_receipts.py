#!/usr/bin/env python3
"""Append authoritative Dialpad SMS send receipts to a JSONL ledger."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import sms_approval


DEFAULT_LEDGER_PATH = Path("/data/.openclaw/state/dialpad/sms-receipts.jsonl")
LEDGER_PATH = Path(os.environ.get("DIALPAD_SMS_RECEIPT_LEDGER", DEFAULT_LEDGER_PATH))
MAX_LEDGER_BYTES = 5 * 1024 * 1024

AppendReceiptStatus = Literal["appended", "not_applicable", "append_failed"]


def _ledger_path() -> Path:
    return Path(os.environ.get("DIALPAD_SMS_RECEIPT_LEDGER", str(LEDGER_PATH)))


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
    successful Dialpad SMS response.
    """
    sms_id, delivery_status = sms_approval._extract_send_result(send_result)
    failure_reason = sms_approval._send_result_failure_reason(sms_id, delivery_status)
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

    try:
        _append_jsonl(_ledger_path(), payload)
    except Exception as exc:  # noqa: BLE001 - receipt persistence is observational only.
        print(f"Warning: failed to append Dialpad SMS receipt ledger: {exc}", file=sys.stderr)
        return "append_failed"

    return "appended"
