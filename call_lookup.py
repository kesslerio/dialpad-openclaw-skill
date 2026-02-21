#!/usr/bin/env python3
"""Shared Dialpad call selection and HTTP helpers."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

DIALPAD_API_BASE = "https://dialpad.com/api/v2"


class DialpadConfigError(Exception):
    """Raised when required local configuration is missing."""


class DialpadApiError(Exception):
    """Raised when a Dialpad API request fails."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def require_api_key() -> str:
    api_key = os.environ.get("DIALPAD_API_KEY")
    if not api_key:
        raise DialpadConfigError("DIALPAD_API_KEY environment variable not set")
    return api_key


def api_get(path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
    api_key = require_api_key()
    url = f"{DIALPAD_API_BASE}{path}"
    if query:
        query = {k: v for k, v in query.items() if v is not None}
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else ""
        message = body.strip() or str(exc)
        try:
            parsed = json.loads(body) if body else {}
            if isinstance(parsed, dict):
                message = (
                    parsed.get("error", {}).get("message")
                    or parsed.get("message")
                    or message
                )
        except json.JSONDecodeError:
            pass
        raise DialpadApiError(f"Dialpad API error (HTTP {exc.code}): {message}", exc.code) from exc
    except urllib.error.URLError as exc:
        raise DialpadApiError(f"Network error: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise DialpadApiError(f"Invalid JSON response: {exc}") from exc


def extract_call_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("items"), list):
        return [item for item in payload["items"] if isinstance(item, dict)]
    if isinstance(payload.get("calls"), list):
        return [item for item in payload["calls"] if isinstance(item, dict)]
    if isinstance(payload.get("results"), list):
        return [item for item in payload["results"] if isinstance(item, dict)]
    return []


def list_calls(max_pages: int = 25) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    cursor: str | None = None

    for _ in range(max_pages):
        response = api_get("/call", {"cursor": cursor} if cursor else None)
        items = extract_call_items(response)
        calls.extend(items)
        cursor = response.get("cursor") if isinstance(response, dict) else None
        if not cursor:
            break

    return calls


def _extract_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, dict):
        strings: list[str] = []
        for nested in value.values():
            strings.extend(_extract_strings(nested))
        return strings
    if isinstance(value, list):
        strings = []
        for nested in value:
            strings.extend(_extract_strings(nested))
        return strings
    return []


def _call_text_fields(call: dict[str, Any]) -> list[str]:
    fields = [
        call.get("external_number"),
        call.get("from_number"),
        call.get("to_number"),
        call.get("contact"),
    ]
    strings: list[str] = []
    for field in fields:
        strings.extend(_extract_strings(field))
    return strings


def _parse_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            pass
        iso = stripped.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(iso).timestamp()
        except ValueError:
            return None
    return None


def _call_sort_key(call: dict[str, Any], original_index: int) -> tuple[float, int]:
    time_fields = (
        "date_started",
        "started_at",
        "start_time",
        "date_created",
        "date",
    )
    ts_candidates = [_parse_timestamp(call.get(name)) for name in time_fields]
    ts = max((value for value in ts_candidates if value is not None), default=float("-inf"))
    return (ts, -original_index)


def select_call(calls: list[dict[str, Any]], with_value: str | None = None) -> dict[str, Any] | None:
    if not calls:
        return None

    selected = calls
    if with_value:
        needle = with_value.lower()
        selected = [
            call
            for call in calls
            if any(needle in text.lower() for text in _call_text_fields(call))
        ]
        if not selected:
            return None

    ranked = sorted(
        enumerate(selected),
        key=lambda pair: _call_sort_key(pair[1], pair[0]),
        reverse=True,
    )
    return ranked[0][1] if ranked else None


def resolve_call_id(call_id: str | None, use_last: bool, with_value: str | None) -> str:
    if call_id:
        return call_id
    if not use_last:
        raise DialpadApiError("Either --call-id or --last is required")

    call = select_call(list_calls(), with_value=with_value)
    if not call:
        if with_value:
            raise DialpadApiError(f"No calls found matching --with {with_value!r}")
        raise DialpadApiError("No calls found")

    found_call_id = str(call.get("call_id") or call.get("id") or "").strip()
    if not found_call_id:
        raise DialpadApiError("Selected call is missing call_id")
    return found_call_id
