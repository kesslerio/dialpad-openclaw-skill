#!/usr/bin/env python3
"""Shared helpers for Dialpad compatibility wrappers.

Provides common utilities (auth, CLI invocation, error handling) used by
the bin/ wrapper scripts that bridge legacy script interfaces to the
generated OpenAPI CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GENERATED_DIALPAD = ROOT / "generated" / "dialpad"
SCHEMA_VERSION = "1"
PROFILE_ENV_KEYS = {
    "work": "DIALPAD_PROFILE_WORK_FROM",
    "sales": "DIALPAD_PROFILE_SALES_FROM",
}
COMMAND_IDS = {
    "send_sms.send": "send_sms.send",
    "make_call.call": "make_call.call",
    "lookup_contact.lookup": "lookup_contact.lookup",
    "create_contact.upsert": "create_contact.upsert",
    "update_contact.update": "update_contact.update",
    "send_group_intro.send": "send_group_intro.send",
    "create_sms_webhook.create": "create_sms_webhook.create",
    "create_sms_webhook.list": "create_sms_webhook.list",
    "create_sms_webhook.delete": "create_sms_webhook.delete",
    "create_sms_webhook.webhooks_list": "create_sms_webhook.webhooks_list",
    "create_sms_webhook.webhooks_delete": "create_sms_webhook.webhooks_delete",
    "export_sms.export": "export_sms.export",
}
ERROR_CODES = {
    "missing_generated_cli",
    "auth_missing",
    "validation_failed",
    "invalid_argument",
    "not_found",
    "conflict",
    "upstream_error",
    "network_error",
    "timeout",
    "partial_success",
    "internal_error",
}
E164_RE = re.compile(r"^\+\d{7,15}$")


class WrapperError(Exception):
    """Raised when wrapper execution fails."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        meta: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.meta = meta or {}


class WrapperArgumentParser(argparse.ArgumentParser):
    """Argparse parser that raises WrapperError instead of exiting."""

    def error(self, message: str) -> None:
        raise WrapperError(message, code="invalid_argument", retryable=False)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status == 0:
            raise SystemExit(0)
        detail = (message or "").strip() or "invalid arguments"
        raise WrapperError(detail, code="invalid_argument", retryable=False)


def _normalize_profile(profile: str | None) -> str | None:
    if not profile:
        return None

    normalized = profile.strip().lower()
    if normalized in PROFILE_ENV_KEYS:
        return normalized

    raise WrapperError(f"Invalid profile '{profile}'. Use work or sales.")


def _validate_e164(number: str, source: str) -> str:
    normalized = number.strip()
    if not normalized or not E164_RE.match(normalized):
        raise WrapperError(f"Invalid sender number from {source}: '{number}'. Use E.164 format like +14155551234.")
    return normalized


def _profile_from_env(profile: str) -> str:
    env_key = PROFILE_ENV_KEYS[profile]
    raw = os.environ.get(env_key, "")
    if not raw.strip():
        raise WrapperError(
            f"Profile '{profile}' is not configured. Set {env_key} to an E.164 number."
        )
    return _validate_e164(raw, env_key)


def resolve_sender(
    from_number: str | None,
    profile: str | None,
    *,
    allow_profile_mismatch: bool = False,
) -> tuple[str, str]:
    resolved_profile = _normalize_profile(profile)

    if from_number and resolved_profile:
        normalized_from = _validate_e164(from_number, "--from")
        mapped = _profile_from_env(resolved_profile)
        if mapped != normalized_from and not allow_profile_mismatch:
            raise WrapperError(
                "--from conflicts with --profile. "
                f"Use --allow-profile-mismatch to keep --from={normalized_from} while using --profile={resolved_profile}"
            )
        return normalized_from, f"--from with matching --profile={resolved_profile}"

    if from_number:
        return _validate_e164(from_number, "--from"), "--from"

    if resolved_profile:
        return _profile_from_env(resolved_profile), f"--profile={resolved_profile}"

    default_from = os.environ.get("DIALPAD_DEFAULT_FROM_NUMBER", "")
    if default_from.strip():
        return _validate_e164(default_from, "DIALPAD_DEFAULT_FROM_NUMBER"), "DIALPAD_DEFAULT_FROM_NUMBER"

    default_profile = _normalize_profile(os.environ.get("DIALPAD_DEFAULT_PROFILE"))
    if default_profile:
        return _profile_from_env(default_profile), f"DIALPAD_DEFAULT_PROFILE={default_profile}"

    raise WrapperError(
        "No sender resolved. Provide --from, --profile, or set "
        "DIALPAD_DEFAULT_FROM_NUMBER / DIALPAD_DEFAULT_PROFILE."
    )



def generated_cli_available() -> bool:
    return GENERATED_DIALPAD.exists()


def require_generated_cli() -> None:
    if generated_cli_available():
        return
    raise WrapperError(
        f"Generated CLI not found at {GENERATED_DIALPAD}",
        code="missing_generated_cli",
        retryable=False,
    )


def require_api_key() -> None:
    if os.environ.get("DIALPAD_API_KEY") or os.environ.get("DIALPAD_TOKEN"):
        return
    raise WrapperError("DIALPAD_API_KEY environment variable not set", code="auth_missing", retryable=False)



def _env_with_auth() -> dict[str, str]:
    env = os.environ.copy()
    api_key = env.get("DIALPAD_API_KEY")
    if api_key and not env.get("DIALPAD_TOKEN"):
        env["DIALPAD_TOKEN"] = api_key
    return env



def run_generated(args: list[str], capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    require_generated_cli()

    cmd = [str(GENERATED_DIALPAD), *args]
    try:
        return subprocess.run(
            cmd,
            env=_env_with_auth(),
            text=True,
            capture_output=capture_output,
        )
    except OSError as exc:
        raise WrapperError(f"Failed to execute generated CLI: {exc}") from exc



def run_generated_json(args: list[str]) -> Any:
    cmd = ["--output", "json", *args]
    proc = run_generated(cmd, capture_output=True)
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or "generated command failed"
        raise WrapperError(message, code="upstream_error", retryable=True)

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise WrapperError(f"Failed to parse JSON output: {exc}") from exc



def print_wrapper_error(err: Exception) -> None:
    print(f"Error: {err}", file=sys.stderr)


def build_meta(wrapper: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    meta: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "wrapper": wrapper,
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if extra:
        meta.update(extra)
    return meta


def emit_success(
    command: str,
    wrapper: str,
    data: dict[str, object],
    meta_extra: dict[str, object] | None = None,
) -> None:
    print(
        json.dumps(
            {
                "ok": True,
                "command": command,
                "data": data,
                "meta": build_meta(wrapper, meta_extra),
            },
            indent=2,
        )
    )


def emit_error(
    command: str,
    wrapper: str,
    code: str,
    message: str,
    retryable: bool,
    meta_extra: dict[str, object] | None = None,
) -> None:
    resolved_code = code if code in ERROR_CODES else "internal_error"
    print(
        json.dumps(
            {
                "ok": False,
                "command": command,
                "error": {
                    "code": resolved_code,
                    "message": message,
                    "retryable": retryable,
                },
                "meta": build_meta(wrapper, meta_extra),
            },
            indent=2,
        )
    )


def classify_wrapper_error(message: str) -> tuple[str, bool]:
    lowered = message.lower()
    if "generated cli not found" in lowered:
        return "missing_generated_cli", False
    if "api key environment variable not set" in lowered:
        return "auth_missing", False
    if "timed out" in lowered:
        return "timeout", True
    if "network error" in lowered:
        return "network_error", True
    if "partial success" in lowered:
        return "partial_success", False
    if "not found" in lowered:
        return "not_found", False
    if "conflict" in lowered or "ambiguous" in lowered:
        return "conflict", False
    if (
        "invalid " in lowered
        or "missing " in lowered
        or "no update fields provided" in lowered
        or "requires" in lowered
        or "refusing to send" in lowered
        or "must be" in lowered
    ):
        return "validation_failed", False
    if "request failed" in lowered or "dialpad api error" in lowered:
        return "upstream_error", True
    return "internal_error", False


def handle_wrapper_exception(command: str, wrapper: str, err: Exception, json_mode: bool) -> int:
    if json_mode:
        if isinstance(err, WrapperError) and err.code:
            code = err.code if err.code in ERROR_CODES else "internal_error"
            retryable = bool(err.retryable) if err.retryable is not None else False
            meta_extra = err.meta
        else:
            code, retryable = classify_wrapper_error(str(err))
            meta_extra = err.meta if isinstance(err, WrapperError) else None
        emit_error(command, wrapper, code, str(err), retryable, meta_extra=meta_extra)
        return 2

    print_wrapper_error(err)
    return 2
