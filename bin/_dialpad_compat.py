#!/usr/bin/env python3
"""Shared helpers for Dialpad compatibility wrappers.

Provides common utilities (auth, CLI invocation, error handling) used by
the bin/ wrapper scripts that bridge legacy script interfaces to the
generated OpenAPI CLI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GENERATED_DIALPAD = ROOT / "generated" / "dialpad"


class WrapperError(Exception):
    """Raised when wrapper execution fails."""



def generated_cli_available() -> bool:
    return GENERATED_DIALPAD.exists()



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
    if not generated_cli_available():
        raise WrapperError(f"Generated CLI not found at {GENERATED_DIALPAD}")

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



def run_legacy(script_name: str, forwarded_args: list[str]) -> int:
    legacy = ROOT / script_name
    if not legacy.exists():
        print(f"Error: fallback script not found: {legacy}", file=sys.stderr)
        return 2

    proc = subprocess.run([sys.executable, str(legacy), *forwarded_args])
    return proc.returncode



def print_wrapper_error(err: Exception) -> None:
    print(f"Error: {err}", file=sys.stderr)
