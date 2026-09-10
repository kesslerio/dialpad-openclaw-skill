from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import send_sms  # noqa: E402


def test_supported_send_records_exact_successful_observation() -> None:
    captured: dict[str, object] = {}

    def record(result, *, to_numbers, from_number, body):
        captured.update(
            {
                "result": result,
                "to_numbers": to_numbers,
                "from_number": from_number,
                "body": body,
            }
        )
        return {"memory_sync": "pending", "memory_outbox": "/tmp/test-outbox.jsonl"}

    with patch.object(send_sms, "require_generated_cli"), \
            patch.object(send_sms, "resolve_sender", return_value=("+14155201316", "--from")), \
            patch.object(send_sms, "run_generated_json", return_value={"id": "msg-send-1", "message_status": "pending"}), \
            patch.object(send_sms, "require_api_key"), \
            patch.object(send_sms, "record_outbound_observation", side_effect=record):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(sys, "argv", [
            "bin/send_sms.py",
            "--to",
            "+14155550111",
            "--message",
            "Exact $body",
            "--from",
            "+14155201316",
            "--json",
        ]), redirect_stdout(stdout), redirect_stderr(stderr):
            code = send_sms.main()

    assert code == 0
    assert stderr.getvalue() == ""
    parsed = json.loads(stdout.getvalue())
    assert parsed["meta"]["memory_sync"] == "pending"
    assert captured == {
        "result": {"id": "msg-send-1", "message_status": "pending"},
        "to_numbers": ["+14155550111"],
        "from_number": "+14155201316",
        "body": "Exact $body",
    }
