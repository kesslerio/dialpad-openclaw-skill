from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

LIST_CALLS_SPEC = importlib.util.spec_from_file_location(
    "bin_list_calls_wrapper",
    Path(__file__).resolve().parent.parent / "bin" / "list_calls.py",
)
assert LIST_CALLS_SPEC is not None and LIST_CALLS_SPEC.loader is not None
list_calls_wrapper = importlib.util.module_from_spec(LIST_CALLS_SPEC)
LIST_CALLS_SPEC.loader.exec_module(list_calls_wrapper)


class _FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ListCallsWrapperTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        with patch.object(sys, "argv", argv):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = list_calls_wrapper.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_text_mode_prints_empty_message_when_no_calls(self):
        with patch.object(list_calls_wrapper, "require_api_key"), patch.object(
            list_calls_wrapper, "fetch_calls", return_value=[]
        ):
            code, out, err = self._run(["bin/list_calls.py", "--today"])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out.strip(), "No calls found for the requested filters.")

    def test_json_mode_includes_filters_and_window(self):
        with patch.object(list_calls_wrapper, "require_api_key"), patch.object(
            list_calls_wrapper,
            "fetch_calls",
            return_value=[{"call_id": "call-1", "date_started": 1742900400000, "duration": 3000}],
        ):
            code, out, err = self._run(["bin/list_calls.py", "--hours", "6", "--limit", "1", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = json.loads(out)
        self.assertEqual(parsed["data"]["count"], 1)
        self.assertEqual(parsed["data"]["filters"]["hours"], 6)
        self.assertFalse(parsed["data"]["filters"]["today"])
        self.assertIn("started_after_ms", parsed["data"]["window"])
        self.assertEqual(parsed["data"]["calls"][0]["call_id"], "call-1")

    def test_json_mode_wraps_csv_write_failures(self):
        with patch.object(list_calls_wrapper, "require_api_key"), patch.object(
            list_calls_wrapper, "fetch_calls", return_value=[]
        ), patch.object(list_calls_wrapper, "write_csv", side_effect=OSError("Permission denied")):
            code, out, err = self._run(["bin/list_calls.py", "--output", "/tmp/calls.csv", "--json"])

        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = json.loads(out)
        self.assertEqual(parsed["error"]["code"], "invalid_argument")
        self.assertIn("Failed to write CSV output", parsed["error"]["message"])

    def test_today_json_mode_nulls_hours_filter(self):
        with patch.object(list_calls_wrapper, "require_api_key"), patch.object(
            list_calls_wrapper, "fetch_calls", return_value=[]
        ):
            code, out, err = self._run(["bin/list_calls.py", "--today", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = json.loads(out)
        self.assertTrue(parsed["data"]["filters"]["today"])
        self.assertIsNone(parsed["data"]["filters"]["hours"])

    def test_token_only_environment_reaches_underlying_http_helper(self):
        captured_headers = {}

        def fake_urlopen(request):
            captured_headers["authorization"] = request.headers.get("Authorization")
            return _FakeResponse({"items": []})

        with patch.dict(list_calls_wrapper._SCRIPT.os.environ, {"DIALPAD_TOKEN": "token-only"}, clear=True), patch.object(
            list_calls_wrapper._SCRIPT.urllib.request,
            "urlopen",
            side_effect=fake_urlopen,
        ):
            code, out, err = self._run(["bin/list_calls.py", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(captured_headers["authorization"], "Bearer token-only")

    def test_local_mode_reads_from_call_sqlite_without_api_key(self):
        stored_calls = [
            {
                "call_id": "call-local-99",
                "direction": "inbound",
                "contact_number": "4155550123",
                "contact_name": "Jane",
                "from_number": "+14155550123",
                "to_number": "+14155201316",
                "date_started": 1770000000000,
                "date_ended": 1770000060000,
                "duration": 60,
                "call_state": "completed",
                "transcript_present": True,
                "transcript_url": "https://dialpad.com/review/call-local-99",
            }
        ]
        with patch("call_sqlite.list_stored_calls", return_value=stored_calls):
            code, out, err = self._run(["bin/list_calls.py", "--local", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = json.loads(out)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["data"]["count"], 1)
        self.assertEqual(parsed["data"]["calls"][0]["call_id"], "call-local-99")


if __name__ == "__main__":
    unittest.main()

