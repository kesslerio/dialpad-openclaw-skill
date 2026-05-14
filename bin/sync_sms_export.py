#!/usr/bin/env python3
"""Sync Dialpad Stats text exports into local SQLite SMS history."""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_PATH = str(ROOT / "scripts")
if SCRIPTS_PATH not in sys.path:
    sys.path.append(SCRIPTS_PATH)

from _dialpad_compat import (  # noqa: E402
    COMMAND_IDS,
    WrapperArgumentParser,
    WrapperError,
    emit_success,
    handle_wrapper_exception,
    print_wrapper_error,
    require_api_key,
    require_generated_cli,
    run_generated_json,
)
from export_sms import build_create_args, download_file, poll_for_completion  # noqa: E402
from sms_sqlite import init_db, store_message  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = WrapperArgumentParser(description="Sync Dialpad text export rows into local SMS SQLite history")
    parser.add_argument("--start-date", dest="start_date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", dest="end_date", help="End date (YYYY-MM-DD)")
    parser.add_argument("--office-id", dest="office_id", help="Office ID filter")
    parser.add_argument("--input-csv", help="Import an existing Dialpad texts CSV instead of creating a new export")
    parser.add_argument("--poll-interval", type=int, default=5, help="Polling interval seconds")
    parser.add_argument("--timeout", type=int, default=600, help="Timeout seconds")
    parser.add_argument("--dry-run", action="store_true", help="Parse and summarize without writing SQLite")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser


def parse_export_timestamp(value: str | None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            continue
    raise WrapperError(f"Invalid export date '{raw}'", code="invalid_argument", retryable=False)


def normalize_export_direction(value: str | None) -> str | None:
    direction = str(value or "").strip().lower()
    if direction == "internal":
        return "outbound"
    if direction == "external":
        return "inbound"
    if direction in {"inbound", "outbound"}:
        return direction
    return None


def export_row_to_webhook_payload(row: dict[str, str]) -> dict[str, Any] | None:
    direction = normalize_export_direction(row.get("direction"))
    if direction is None:
        return None

    message_id = str(row.get("message_id") or "").strip()
    if not message_id:
        return None

    from_phone = str(row.get("from_phone") or "").strip()
    to_phone = str(row.get("to_phone") or "").strip()
    if not from_phone or not to_phone:
        return None

    contact_name = str(row.get("name") or "").strip()
    return {
        "id": int(message_id),
        "direction": direction,
        "from_number": from_phone,
        "to_number": [to_phone],
        "text": "",
        "message_status": "exported",
        "message_delivery_result": None,
        "mms": str(row.get("mms") or "").strip().lower() in {"1", "true", "yes"},
        "created_date": parse_export_timestamp(row.get("date")),
        "contact": {"name": contact_name} if contact_name else {},
    }


def message_exists(conn: Any, dialpad_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM messages WHERE dialpad_id = ? LIMIT 1", (dialpad_id,)).fetchone()
    return row is not None


def import_csv(path: str, *, dry_run: bool = False) -> dict[str, int]:
    counts = {
        "rows": 0,
        "imported": 0,
        "skipped_existing": 0,
        "skipped_invalid": 0,
    }
    conn = init_db()
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                counts["rows"] += 1
                payload = export_row_to_webhook_payload(row)
                if payload is None:
                    counts["skipped_invalid"] += 1
                    continue
                if message_exists(conn, payload["id"]):
                    counts["skipped_existing"] += 1
                    continue
                if not dry_run:
                    store_message(conn, payload, is_new=False)
                counts["imported"] += 1
    finally:
        conn.close()
    return counts


def create_export(args: argparse.Namespace, output_path: str) -> dict[str, Any]:
    created = run_generated_json(build_create_args(args.start_date, args.end_date, args.office_id))
    request_id = created.get("request_id") or created.get("id")
    if not request_id:
        raise WrapperError(f"Export request did not return request_id: {created}")

    final_result = poll_for_completion(
        str(request_id),
        args.timeout,
        args.poll_interval,
        json_mode=True,
    )
    download_url = final_result.get("download_url")
    if not download_url:
        raise WrapperError(f"Export result did not include download_url: {final_result}")
    download_file(str(download_url), output_path)
    return {
        "request_id": request_id,
        "result": final_result,
    }


def main() -> int:
    json_mode = "--json" in sys.argv
    command = COMMAND_IDS["sync_sms_export.sync"]
    wrapper = "sync_sms_export.py"

    try:
        args = build_parser().parse_args()
        json_mode = args.json
        export_info = None
        if args.input_csv:
            csv_path = args.input_csv
        else:
            require_generated_cli()
            require_api_key()
            with tempfile.NamedTemporaryFile(prefix="dialpad-texts-", suffix=".csv", delete=False) as temp:
                csv_path = temp.name
            export_info = create_export(args, csv_path)

        counts = import_csv(csv_path, dry_run=args.dry_run)
        data = {
            **counts,
            "dry_run": args.dry_run,
            "input_csv": csv_path if args.input_csv else None,
            "export": export_info,
        }
        if json_mode:
            emit_success(command, wrapper, data)
            return 0

        print(
            "SMS export sync: "
            f"{counts['imported']} imported, "
            f"{counts['skipped_existing']} existing, "
            f"{counts['skipped_invalid']} invalid, "
            f"{counts['rows']} row(s) scanned"
        )
        return 0
    except WrapperError as err:
        if json_mode:
            return handle_wrapper_exception(command, wrapper, err, True)
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
