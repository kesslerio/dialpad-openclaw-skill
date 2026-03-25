from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from list_calls import CALLS_ENDPOINT, fetch_calls, normalize_duration, to_call_summary


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ListCallsTests(unittest.TestCase):
    def test_calls_endpoint_uses_singular_path(self):
        self.assertTrue(CALLS_ENDPOINT.endswith("/api/v2/call"))

    def test_normalize_duration_treats_values_as_milliseconds(self):
        self.assertEqual(normalize_duration({"duration": 5000}), 5)
        self.assertEqual(normalize_duration({"duration": 120000}), 120)
        self.assertEqual(normalize_duration({"total_duration": 9000}), 9)

    def test_fetch_calls_applies_missed_filter_before_limit(self):
        page1 = {
            "items": [
                {"state": "answered", "duration": 8000, "date_started": 1},
            ],
            "cursor": "next-page",
        }
        page2 = {
            "items": [
                {"state": "missed", "duration": 0, "date_started": 2},
            ],
        }

        responses = [_FakeResponse(page1), _FakeResponse(page2)]

        with patch("list_calls.urllib.request.urlopen", side_effect=responses):
            rows = fetch_calls(0, 999999, limit=1, missed_only=True)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("state"), "missed")

    def test_fetch_calls_accepts_token_only_environment(self):
        captured_headers = {}

        def fake_urlopen(request):
            captured_headers["authorization"] = request.headers.get("Authorization")
            return _FakeResponse({"items": []})

        with patch.dict("list_calls.os.environ", {"DIALPAD_TOKEN": "token-only"}, clear=True), patch(
            "list_calls.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            rows = fetch_calls(0, 999999, limit=1)

        self.assertEqual(rows, [])
        self.assertEqual(captured_headers["authorization"], "Bearer token-only")

    def test_to_call_summary_exposes_agent_facing_fields(self):
        summary = to_call_summary(
            {
                "call_id": "call-123",
                "date_started": 1742900400000,
                "duration": 65000,
                "direction": "outbound",
                "state": "completed",
                "contact": {"name": "Taylor Prospect"},
                "entry_point_target": {"name": "Sales Line"},
                "recording_url": "https://example.com/recording.mp3",
                "disposition_name": "demo_scheduled",
            }
        )

        self.assertEqual(summary["call_id"], "call-123")
        self.assertEqual(summary["started_at"], "2025-03-25T11:00:00Z")
        self.assertEqual(summary["contact"], "Taylor Prospect")
        self.assertEqual(summary["direction"], "outbound")
        self.assertEqual(summary["duration_seconds"], 65)
        self.assertEqual(summary["duration_display"], "1:05")
        self.assertEqual(summary["status"], "answered")
        self.assertEqual(summary["line"], "Sales Line")
        self.assertEqual(summary["recording_url"], "https://example.com/recording.mp3")
        self.assertEqual(summary["outcome"], "demo_scheduled")


if __name__ == "__main__":
    unittest.main()
