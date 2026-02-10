#!/usr/bin/env python3
"""Compatibility wrapper: export_sms.py -> dialpad sms export."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import date, datetime

from _dialpad_compat import (
    generated_cli_available,
    print_wrapper_error,
    require_api_key,
    run_generated_json,
    run_legacy,
    WrapperError,
)



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export historical SMS via Dialpad stats API")
    parser.add_argument("--start-date", dest="start_date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", dest="end_date", help="End date (YYYY-MM-DD)")
    parser.add_argument("--office-id", dest="office_id", help="Office ID filter")
    parser.add_argument("--output", "-o", help="Output file path")
    parser.add_argument("--poll-interval", type=int, default=5, help="Polling interval seconds")
    parser.add_argument("--timeout", type=int, default=600, help="Timeout seconds")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser



def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise WrapperError(f"Invalid date '{value}', expected YYYY-MM-DD") from exc



def to_days_ago(value: date) -> int:
    delta = (date.today() - value).days
    if delta < 0:
        raise WrapperError(f"Date {value.isoformat()} is in the future")
    return delta



def build_payload(start_date: str | None, end_date: str | None, office_id: str | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "export_type": "records",
        "stat_type": "texts",
    }

    start = parse_iso_date(start_date)
    end = parse_iso_date(end_date)

    if start and end and start > end:
        raise WrapperError("start-date must be before or equal to end-date")

    if start:
        payload["days_ago_start"] = to_days_ago(start)
    if end:
        payload["days_ago_end"] = to_days_ago(end)
    if office_id:
        payload["office_id"] = office_id

    return payload



def download_file(url: str, output_path: str) -> None:
    try:
        with urllib.request.urlopen(url) as response:
            content = response.read()
    except Exception as exc:  # noqa: BLE001
        raise WrapperError(f"Failed to download export file: {exc}") from exc

    with open(output_path, "wb") as handle:
        handle.write(content)



def main() -> int:
    if not generated_cli_available():
        return run_legacy("export_sms.py", sys.argv[1:])

    args = build_parser().parse_args()

    try:
        require_api_key()

        payload = build_payload(args.start_date, args.end_date, args.office_id)
        created = run_generated_json(["sms", "export", "--data", json.dumps(payload)])

        request_id = created.get("request_id")
        if not request_id:
            raise WrapperError(f"Export request did not return request_id: {created}")

        print(f"Export job created: {request_id}")
        print("Polling for completion...")

        started = time.time()
        final_result = None

        while (time.time() - started) <= args.timeout:
            status = run_generated_json(["stats", "stats.get", "--id", str(request_id)])
            state = status.get("status")
            print(f"   Status: {state}")

            if state == "complete":
                final_result = status
                break
            if state == "failed":
                raise WrapperError("Export failed")

            time.sleep(max(1, args.poll_interval))

        if not final_result:
            raise WrapperError(f"Timed out after {args.timeout} seconds")

        download_url = final_result.get("download_url")
        if args.output and download_url:
            print(f"   Downloading to {args.output}...")
            download_file(download_url, args.output)
            print(f"   Saved to {args.output}")

        if args.json:
            print(json.dumps(final_result, indent=2))
        else:
            print("Export completed!")
            if args.output:
                print(f"   File: {args.output}")
            print(f"   Status: {final_result.get('status')}")

        return 0
    except WrapperError as err:
        print_wrapper_error(err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
