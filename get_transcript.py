#!/usr/bin/env python3
"""Retrieve Dialpad call transcripts."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from call_lookup import (
    DialpadApiError,
    DialpadConfigError,
    api_get,
    resolve_call_id,
)


def format_transcript(payload: dict[str, Any]) -> str:
    string_candidates = (
        payload.get("transcript"),
        payload.get("text"),
        payload.get("full_text"),
        payload.get("content"),
    )
    for candidate in string_candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    list_candidates = []
    for key in ("utterances", "segments", "items", "transcript"):
        value = payload.get(key)
        if isinstance(value, list):
            list_candidates = value
            break

    lines: list[str] = []
    for item in list_candidates:
        if not isinstance(item, dict):
            continue
        text = (
            item.get("text")
            or item.get("transcript")
            or item.get("content")
            or item.get("utterance")
        )
        if not isinstance(text, str) or not text.strip():
            continue
        speaker = (
            item.get("speaker")
            or item.get("speaker_name")
            or item.get("participant")
            or item.get("role")
        )
        if isinstance(speaker, str) and speaker.strip():
            lines.append(f"{speaker.strip()}: {text.strip()}")
        else:
            lines.append(text.strip())

    return "\n".join(lines).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrieve a Dialpad call transcript")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--call-id", help="Dialpad call ID")
    group.add_argument("--last", action="store_true", help="Use the most recent call")
    parser.add_argument(
        "--with",
        dest="with_value",
        help="Filter most recent call by phone number or contact substring",
    )
    parser.add_argument("--raw-json", action="store_true", help="Print raw JSON response")
    args = parser.parse_args()

    try:
        chosen_call_id = resolve_call_id(args.call_id, args.last, args.with_value)
        transcript = api_get(f"/transcripts/{chosen_call_id}")
        if args.raw_json:
            print(json.dumps(transcript, indent=2))
            return 0

        body = format_transcript(transcript)
        print(f"Transcript for call {chosen_call_id}")
        if body:
            print()
            print(body)
        else:
            print()
            print("Transcript data returned, but no readable transcript text was found.")
        return 0
    except DialpadConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except DialpadApiError as exc:
        if exc.status_code == 404:
            call_ref = args.call_id or "(most recent selection)"
            print(f"Transcript unavailable for call {call_ref}.", file=sys.stderr)
            return 2
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
