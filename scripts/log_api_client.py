#!/usr/bin/env python3
"""Small authenticated client for the private interaction-log API."""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from typing import Any



def _load_env_file() -> None:
    """Best-effort load of ~/.config/dialpad.env for shared-log clients."""
    explicit = os.environ.get("DIALPAD_ENV_FILE", "").strip()
    path = Path(explicit).expanduser() if explicit else Path.home() / ".config" / "dialpad.env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key not in os.environ or os.environ.get(key, "") == "":
            os.environ[key] = value


class LogApiError(RuntimeError):
    def __init__(self, message: str, *, code: str = "network_error", retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def configured_log_url() -> str | None:
    _load_env_file()
    value = os.environ.get("DIALPAD_LOG_URL", "").strip().rstrip("/")
    return value or None


def _safe_message(message: Any, token: str) -> str:
    text = str(message or "request failed")
    return text.replace(token, "[redacted]")


def request_json(
    path: str,
    *,
    method: str = "GET",
    query: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    base_url = configured_log_url()
    if not base_url:
        raise LogApiError("DIALPAD_LOG_URL is not configured", code="invalid_argument", retryable=False)
    token = os.environ.get("DIALPAD_LOG_TOKEN", "")
    if not token.strip():
        raise LogApiError("DIALPAD_LOG_TOKEN is required when DIALPAD_LOG_URL is set", code="auth_missing", retryable=False)
    url = f"{base_url}{path}"
    if query:
        encoded_query = urllib.parse.urlencode({key: value for key, value in query.items() if value is not None})
        if encoded_query:
            url = f"{url}?{encoded_query}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
        method=method,
    )
    request_timeout = timeout if timeout is not None else float(os.environ.get("DIALPAD_LOG_TIMEOUT", "5"))
    try:
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace") if error.fp else ""
        try:
            parsed_error = json.loads(raw)
        except json.JSONDecodeError:
            parsed_error = {}
        detail = parsed_error.get("error", {}).get("message") if isinstance(parsed_error, dict) else None
        raise LogApiError(
            _safe_message(detail or f"interaction log API returned HTTP {error.code}", token),
            code=(parsed_error.get("error", {}).get("code") if isinstance(parsed_error, dict) else None) or "upstream_error",
            retryable=error.code >= 500,
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise LogApiError(_safe_message(error, token), code="network_error", retryable=True) from error
    try:
        response_data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LogApiError("interaction log API returned invalid JSON", code="upstream_error", retryable=True) from error
    if not isinstance(response_data, dict) or response_data.get("ok") is not True:
        error_data = response_data.get("error") if isinstance(response_data, dict) else {}
        if not isinstance(error_data, dict):
            error_data = {}
        raise LogApiError(
            _safe_message(error_data.get("message") or "interaction log API request failed", token),
            code=str(error_data.get("code") or "upstream_error"),
            retryable=bool(error_data.get("retryable", True)),
        )
    return response_data


def get_data(path: str, *, query: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    response = request_json(path, query=query)
    data = response.get("data")
    if not isinstance(data, dict):
        raise LogApiError("interaction log API returned invalid data", code="upstream_error", retryable=True)
    meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
    return data, meta

def record_message(observation: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    response = request_json("/v1/sms/record", method="POST", payload=observation)
    data = response.get("data")
    if not isinstance(data, dict):
        raise LogApiError("interaction log API returned invalid record data", code="upstream_error", retryable=True)
    meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
    return data, meta
