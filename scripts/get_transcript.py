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
    resolve_call,
    resolve_call_id,
)


def format_transcript(payload: dict[str, Any]) -> str:
    string_candidates = (
        payload.get("transcript"),
        payload.get("transcription_text"),
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


def _call_metadata(call: dict[str, Any]) -> dict[str, Any]:
    return {
        key: call.get(key)
        for key in (
            "call_id",
            "id",
            "date_started",
            "external_number",
            "phone_number",
            "direction",
            "state",
            "duration",
            "total_duration",
            "contact",
        )
        if call.get(key) is not None
    }


def _available_result(
    call_id: str,
    transcript_text: str,
    *,
    source: str,
    transcript_review_url: str | None = None,
    call: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "call_id": call_id,
        "available": True,
        "transcript_text": transcript_text,
        "transcript_review_url": transcript_review_url,
        "source": source,
        "unavailable_reason": None,
    }
    if call:
        result["call"] = _call_metadata(call)
    return result


def _unavailable_result(
    call_id: str,
    *,
    reason: str,
    source: str | None = None,
    transcript_review_url: str | None = None,
    call: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "call_id": call_id,
        "available": False,
        "transcript_text": None,
        "transcript_review_url": transcript_review_url,
        "source": source,
        "unavailable_reason": reason,
    }
    if call:
        result["call"] = _call_metadata(call)
    return result


def format_transcript_review_url(payload: dict[str, Any]) -> str | None:
    for key in ("url", "transcript_url", "review_url", "call_review_url", "call_review_share_link"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def get_transcript_review_url(call_id: str) -> str | None:
    try:
        payload = api_get(f"/transcripts/{call_id}/url")
    except DialpadApiError:
        return None
    return format_transcript_review_url(payload)


def get_call_transcript(call_id: str, call: dict[str, Any] | None = None) -> dict[str, Any]:
    """Retrieve a transcript result for one call.

    Missing transcripts are expected Dialpad states, so they return
    ``available: False`` instead of raising. API/configuration failures still
    raise ``DialpadApiError`` / ``DialpadConfigError``.
    """
    transcript_review_url = get_transcript_review_url(call_id)

    try:
        transcript_payload = api_get(f"/transcripts/{call_id}")
    except DialpadApiError as exc:
        if exc.status_code != 404:
            raise
    else:
        transcript_text = format_transcript(transcript_payload)
        if transcript_text:
            return _available_result(
                call_id,
                transcript_text,
                source="transcripts",
                transcript_review_url=transcript_review_url,
                call=call,
            )

    try:
        call_payload = api_get(f"/call/{call_id}")
    except DialpadApiError as exc:
        if exc.status_code == 404:
            return _unavailable_result(
                call_id,
                reason="not_found",
                source="call",
                transcript_review_url=transcript_review_url,
                call=call,
            )
        raise

    transcript_text = format_transcript(call_payload)
    merged_call = call_payload if isinstance(call_payload, dict) else call
    if transcript_text:
        return _available_result(
            call_id,
            transcript_text,
            source="call",
            transcript_review_url=transcript_review_url,
            call=merged_call,
        )
    return _unavailable_result(
        call_id,
        reason="no_transcript",
        source="call",
        transcript_review_url=transcript_review_url,
        call=merged_call,
    )


def resolve_call_transcript(call_id: str | None, use_last: bool, with_value: str | None) -> dict[str, Any]:
    call = resolve_call(call_id, use_last, with_value)
    chosen_call_id = str(call.get("call_id") or call.get("id") or "").strip()
    if not chosen_call_id:
        raise DialpadApiError("Selected call is missing call_id")
    return get_call_transcript(chosen_call_id, call=call)


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
