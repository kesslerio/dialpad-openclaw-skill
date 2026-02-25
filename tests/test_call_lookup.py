from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from call_lookup import select_call


class CallSelectorTests(unittest.TestCase):
    def test_selects_most_recent_without_filter(self):
        calls = [
            {"call_id": "older", "date_started": "2026-01-01T12:00:00Z"},
            {"call_id": "newer", "date_started": "2026-01-01T13:00:00Z"},
        ]
        selected = select_call(calls)
        self.assertIsNotNone(selected)
        self.assertEqual(selected["call_id"], "newer")

    def test_selects_most_recent_matching_external_number(self):
        calls = [
            {"call_id": "a", "date_started": "2026-01-01T10:00:00Z", "external_number": "+14155550111"},
            {"call_id": "b", "date_started": "2026-01-01T11:00:00Z", "external_number": "+14155550222"},
            {"call_id": "c", "date_started": "2026-01-01T12:00:00Z", "external_number": "+14155550111"},
        ]
        selected = select_call(calls, with_value="+14155550111")
        self.assertIsNotNone(selected)
        self.assertEqual(selected["call_id"], "c")

    def test_matches_nested_contact_and_to_number_list(self):
        calls = [
            {
                "call_id": "x",
                "date_started": "2026-01-01T10:00:00Z",
                "to_number": ["+14150000001"],
                "contact": {"name": "Acme Support"},
            },
            {
                "call_id": "y",
                "date_started": "2026-01-01T11:00:00Z",
                "to_number": ["+14150000002"],
                "contact": {"name": "Another Contact"},
            },
        ]
        self.assertEqual(select_call(calls, with_value="acme")["call_id"], "x")
        self.assertEqual(select_call(calls, with_value="00002")["call_id"], "y")

    def test_returns_none_when_no_match(self):
        calls = [
            {"call_id": "x", "date_started": "2026-01-01T10:00:00Z", "external_number": "+14150000001"}
        ]
        self.assertIsNone(select_call(calls, with_value="does-not-match"))

    def test_falls_back_to_input_order_when_timestamps_missing(self):
        calls = [
            {"call_id": "first", "from_number": "+1415"},
            {"call_id": "second", "from_number": "+1416"},
        ]
        selected = select_call(calls)
        self.assertIsNotNone(selected)
        self.assertEqual(selected["call_id"], "first")


if __name__ == "__main__":
    unittest.main()
