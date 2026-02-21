#!/usr/bin/env python3
"""
List recent calls via Dialpad API.

Usage:
    python3 list_calls.py
    python3 list_calls.py --hours 6 --limit 25
    python3 list_calls.py --today --missed
    python3 list_calls.py --today --output recent_calls.csv
"""

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any


DIALPAD_API_KEY = os.environ.get("DIALPAD_API_KEY")
CALLS_ENDPOINT = "https://dialpad.com/api/v2/calls"
DEFAULT_HOURS = 24
DEFAULT_LIMIT = 50
MAX_PAGE_SIZE = 100
MAX_PAGES = 20


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List recent calls via Dialpad API")
    time_group = parser.add_mutually_exclusive_group()
    time_group.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_HOURS,
        help=f"Look back this many hours (default: {DEFAULT_HOURS})",
    )
    time_group.add_argument(
        "--today",
        action="store_true",
        help="Only include calls from local midnight to now",
    )
    parser.add_argument(
        "--missed",
        action="store_true",
        help="Only show missed calls",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Maximum number of calls to return (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--output",
        help="Write CSV output to file instead of table output",
    )
    return parser


def utc_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def compute_window(hours: int, today: bool) -> tuple[int, int]:
    now = datetime.now().astimezone()
    if today:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now - timedelta(hours=hours)
    return utc_ms(start), utc_ms(now)


def extract_items(response: Any) -> tuple[list[dict[str, Any]], str | None]:
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)], None
    if isinstance(response, dict):
        items = response.get("items")
        if isinstance(items, list):
            parsed = [item for item in items if isinstance(item, dict)]
        else:
            parsed = []
        cursor = response.get("cursor")
        return parsed, str(cursor) if cursor else None
    return [], None


def fetch_calls(started_after: int, started_before: int, limit: int) -> list[dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {DIALPAD_API_KEY}",
        "Accept": "application/json",
    }

    calls: list[dict[str, Any]] = []
    cursor: str | None = None

    for _ in range(MAX_PAGES):
        remaining = max(1, limit - len(calls))
        page_size = min(MAX_PAGE_SIZE, remaining)

        params: dict[str, str] = {
            "started_after": str(started_after),
            "started_before": str(started_before),
            "limit": str(page_size),
        }
        if cursor:
            params["cursor"] = cursor

        url = f"{CALLS_ENDPOINT}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(request) as response:
                response_data = response.read().decode("utf-8")
                payload = json.loads(response_data)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8") if exc.fp else ""
            try:
                error_data = json.loads(error_body)
                error_msg = error_data.get("error", {}).get("message", error_body)
            except json.JSONDecodeError:
                error_msg = error_body or str(exc)
            raise RuntimeError(f"Dialpad API error (HTTP {exc.code}): {error_msg}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Network error: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON response from Dialpad API: {exc}") from exc

        items, cursor = extract_items(payload)
        if not items:
            break

        calls.extend(items)
        if len(calls) >= limit:
            break
        if not cursor:
            break

    return calls[:limit]


def pick_first_string(values: list[Any]) -> str:
    for value in values:
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                return cleaned
    return "-"


def to_ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def normalize_duration(call: dict[str, Any]) -> int:
    for key in ("duration", "total_duration"):
        value = call.get(key)
        if value is None:
            continue
        try:
            raw = float(str(value))
        except (TypeError, ValueError):
            continue
        # Dialpad duration values are documented in milliseconds.
        if raw > 10_000:
            return max(0, int(raw / 1000))
        return max(0, int(raw))
    return 0


def format_duration(seconds: int) -> str:
    minutes, secs = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def infer_status(call: dict[str, Any]) -> str:
    state_raw = str(call.get("state") or "").strip().lower()
    duration_seconds = normalize_duration(call)

    if "miss" in state_raw:
        return "missed"
    if state_raw in {"answered", "connected", "completed", "hangup", "ended"}:
        return "answered"
    if duration_seconds > 0:
        return "answered"
    return "missed"


def get_caller(call: dict[str, Any]) -> str:
    contact = call.get("contact") or {}
    if not isinstance(contact, dict):
        contact = {}
    return pick_first_string(
        [
            contact.get("name"),
            call.get("external_display_name"),
            contact.get("phone"),
            call.get("external_number"),
            call.get("phone_number"),
        ]
    )


def get_direction(call: dict[str, Any]) -> str:
    direction = str(call.get("direction") or "").strip().lower()
    if direction in {"inbound", "outbound"}:
        return direction
    return "unknown"


def get_line(call: dict[str, Any]) -> str:
    entry_target = call.get("entry_point_target") or {}
    proxy_target = call.get("proxy_target") or {}
    target = call.get("target") or {}

    if not isinstance(entry_target, dict):
        entry_target = {}
    if not isinstance(proxy_target, dict):
        proxy_target = {}
    if not isinstance(target, dict):
        target = {}

    return pick_first_string(
        [
            entry_target.get("name"),
            entry_target.get("phone"),
            proxy_target.get("name"),
            proxy_target.get("phone"),
            call.get("internal_number"),
            target.get("name"),
            target.get("phone"),
            call.get("group_id"),
        ]
    )


def to_row(call: dict[str, Any]) -> dict[str, str]:
    started_ms = to_ms(call.get("date_started"))
    started = "-"
    if started_ms is not None:
        started = datetime.fromtimestamp(started_ms / 1000).astimezone().strftime("%Y-%m-%d %H:%M")

    duration_seconds = normalize_duration(call)
    status = infer_status(call)

    return {
        "started": started,
        "caller": get_caller(call),
        "direction": get_direction(call),
        "duration": format_duration(duration_seconds),
        "status": status,
        "line": get_line(call),
    }


def render_table(rows: list[dict[str, str]]) -> str:
    headers = ["Started", "Caller", "Direction", "Duration", "Status", "Line"]
    keys = ["started", "caller", "direction", "duration", "status", "line"]

    widths = [len(header) for header in headers]
    for row in rows:
        for i, key in enumerate(keys):
            widths[i] = max(widths[i], len(str(row.get(key, ""))))

    def make_line(values: list[str]) -> str:
        return " | ".join(value.ljust(widths[i]) for i, value in enumerate(values))

    output_lines = [make_line(headers), "-+-".join("-" * width for width in widths)]
    for row in rows:
        output_lines.append(make_line([str(row.get(key, "")) for key in keys]))
    return "\n".join(output_lines)


def write_csv(rows: list[dict[str, str]], output_path: str) -> None:
    fieldnames = ["started", "caller", "direction", "duration", "status", "line"]
    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = build_parser().parse_args()

    if args.hours is not None and args.hours <= 0:
        print("Configuration error: --hours must be greater than 0", file=sys.stderr)
        return 1
    if args.limit <= 0:
        print("Configuration error: --limit must be greater than 0", file=sys.stderr)
        return 1

    if not DIALPAD_API_KEY:
        print("Configuration error: DIALPAD_API_KEY environment variable not set", file=sys.stderr)
        return 1

    started_after, started_before = compute_window(args.hours, args.today)

    try:
        raw_calls = fetch_calls(started_after, started_before, args.limit)
    except RuntimeError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 2

    rows = [to_row(call) for call in raw_calls]
    if args.missed:
        rows = [row for row in rows if row["status"] == "missed"]

    if args.output:
        write_csv(rows, args.output)
        print(f"Saved {len(rows)} call(s) to {args.output}")
        return 0

    if not rows:
        print("No calls found for the requested filters.")
        return 0

    print(render_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
