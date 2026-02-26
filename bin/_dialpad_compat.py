#!/usr/bin/env python3
"""Shared helpers for Dialpad compatibility wrappers.

Provides common utilities (auth, CLI invocation, error handling) used by
the bin/ wrapper scripts that bridge legacy script interfaces to the
generated OpenAPI CLI.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GENERATED_DIALPAD = ROOT / "generated" / "dialpad"
PROFILE_ENV_KEYS = {
    "work": "DIALPAD_PROFILE_WORK_FROM",
    "sales": "DIALPAD_PROFILE_SALES_FROM",
}
E164_RE = re.compile(r"^\+\d{7,15}$")


class WrapperError(Exception):
    """Raised when wrapper execution fails."""


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
    raise WrapperError(f"Generated CLI not found at {GENERATED_DIALPAD}")


def require_api_key() -> None:
    if os.environ.get("DIALPAD_API_KEY") or os.environ.get("DIALPAD_TOKEN"):
        return
    raise WrapperError("DIALPAD_API_KEY environment variable not set")



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
        raise WrapperError(message)

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise WrapperError(f"Failed to parse JSON output: {exc}") from exc



def print_wrapper_error(err: Exception) -> None:
    print(f"Error: {err}", file=sys.stderr)
