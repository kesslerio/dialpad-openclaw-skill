from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import list_calls  # noqa: E402
import list_sms_thread  # noqa: E402


def _run(module, argv: list[str]) -> tuple[int, dict[str, object], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch.object(sys, "argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
        code = module.main()
    return code, json.loads(stdout.getvalue()), stderr.getvalue()


def test_sms_thread_uses_shared_log_url_and_preserves_envelope(monkeypatch) -> None:
    monkeypatch.setenv("DIALPAD_LOG_URL", "http://127.0.0.1:18887")
    with patch.object(
        list_sms_thread,
        "get_data",
        return_value=(
            {
                "phone": "+14155550111",
                "count": 1,
                "outbound_count": 1,
                "inbound_count": 0,
                "has_outbound": True,
                "messages": [{
                    "dialpad_id": "msg-1",
                    "direction": "outbound",
                    "from_number": "+14155201316",
                    "to_number": "+14155550111",
                    "timestamp": 1770000000000,
                    "text": "shared",
                }],
            },
            {},
        )), patch.object(list_sms_thread, "init_db") as init_db:
        code, parsed, err = _run(
            list_sms_thread,
            ["bin/list_sms_thread.py", "--phone", "+14155550111", "--json"],
        )

    assert code == 0
    assert err == ""
    assert parsed["meta"]["history_source"] == "shared_log"
    assert parsed["data"]["messages"][0]["text"] == "shared"
    init_db.assert_not_called()


def test_list_calls_defaults_to_shared_mode_when_log_url_is_set(monkeypatch) -> None:
    monkeypatch.setenv("DIALPAD_LOG_URL", "http://127.0.0.1:18887")
    with patch.object(
        list_calls,
        "get_data",
        return_value=(
            {
                "count": 1,
                "calls": [{
                    "call_id": "call-shared-1",
                    "started_at": "2026-09-09T12:00:00Z",
                    "contact": "Jane",
                    "contact_phone": "4155550123",
                    "direction": "inbound",
                    "duration_seconds": 0,
                    "duration_display": "0:00",
                    "status": "missed",
                    "disposition": "missed",
                    "line": "+14155201316",
                }],
            },
            {},
        )), patch.object(list_calls, "require_api_key") as require_api_key:
        code, parsed, err = _run(
            list_calls,
            ["bin/list_calls.py", "--hours", "6", "--missed", "--json"],
        )

    assert code == 0
    assert err == ""
    assert parsed["meta"]["history_source"] == "shared_log"
    assert parsed["data"]["calls"][0]["call_id"] == "call-shared-1"
    assert parsed["data"]["calls"][0]["status"] == "missed"
    assert parsed["data"]["calls"][0]["started_at"] == "2026-09-09T12:00:00Z"
    assert parsed["data"]["calls"][0]["contact"] == "Jane"
    require_api_key.assert_not_called()


def test_list_calls_live_mode_is_explicit_even_with_shared_url(monkeypatch) -> None:
    monkeypatch.setenv("DIALPAD_LOG_URL", "http://127.0.0.1:18887")
    with patch.object(list_calls, "require_api_key"), patch.object(
        list_calls, "fetch_calls", return_value=[]
    ) as fetch_calls:
        code, parsed, err = _run(list_calls, ["bin/list_calls.py", "--live", "--json"])

    assert code == 0
    assert err == ""
    assert parsed["meta"]["history_source"] == "live_dialpad"
    fetch_calls.assert_called_once()
