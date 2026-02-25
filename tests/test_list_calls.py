from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from list_calls import CALLS_ENDPOINT, fetch_calls, normalize_duration


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


if __name__ == "__main__":
    unittest.main()
