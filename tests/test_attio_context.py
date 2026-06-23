"""Unit tests for the Attio CRM context adapter (S1/U2). HTTP layer fully mocked."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "adapters"))

import attio_context as attio  # noqa: E402


PERSON = {
    "id": {"record_id": "person-1"},
    "values": {
        "associated_deals": [{"target_record_id": "deal-1", "target_object": "deals"}],
        "email_addresses": [{"email_address": "jane@acme.example", "active_until": None}],
    },
}
DEAL = {
    "id": {"record_id": "deal-1"},
    "values": {
        "name": [{"value": "Acme (Inbound Demo Request)", "attribute_type": "text"}],
        "stage": [{"status": {"title": "Demo Booked"}}],
        "associated_company": [{"target_record_id": "co-1", "target_object": "companies"}],
        "demo_scheduled_at": [{"value": "2026-06-20T18:30:00.000000000Z", "attribute_type": "timestamp"}],
    },
}
COMPANY = {"id": {"record_id": "co-1"}, "values": {"name": [{"value": "Acme Corp", "attribute_type": "text"}]}}


def fake_request(method, path, body=None):
    if method == "POST" and path == "/objects/people/records/query":
        return {"data": [PERSON]}
    if method == "GET" and path == "/objects/deals/records/deal-1":
        return {"data": DEAL}
    if method == "GET" and path == "/objects/companies/records/co-1":
        return {"data": COMPANY}
    return {"data": []}


class ParseQueryTests(unittest.TestCase):
    def test_extracts_e164_phone_as_first_token(self):
        phone, rest = attio._parse_query("+14155201316 John Doe Acme Corp")
        self.assertEqual(phone, "+14155201316")
        self.assertEqual(rest, "John Doe Acme Corp")

    def test_no_phone_when_first_token_is_name(self):
        phone, rest = attio._parse_query("John Doe Acme")
        self.assertIsNone(phone)
        self.assertEqual(rest, "John Doe Acme")

    def test_empty_query(self):
        self.assertEqual(attio._parse_query(""), (None, ""))


class ValueExtractionTests(unittest.TestCase):
    def test_text_value(self):
        self.assertEqual(attio._text_value(DEAL["values"], "name"), "Acme (Inbound Demo Request)")
        self.assertIsNone(attio._text_value(DEAL["values"], "missing"))

    def test_status_title(self):
        self.assertEqual(attio._status_title(DEAL["values"], "stage"), "Demo Booked")
        self.assertIsNone(attio._status_title({}, "stage"))

    def test_reference_id(self):
        self.assertEqual(attio._reference_id(DEAL["values"], "associated_company"), "co-1")

    def test_first_prefers_active_over_inactive_leading_entry(self):
        # Attio can list a historical entry (active_until set) BEFORE the active
        # one. _first must skip the stale entry and return the active record.
        values = {"name": [
            {"full_name": "Old Name", "active_until": "2025-01-01T00:00:00Z"},
            {"full_name": "Current Name", "active_until": None},
        ]}
        self.assertEqual(attio._first(values, "name").get("full_name"), "Current Name")

    def test_first_falls_back_to_first_dict_when_none_active(self):
        # No entry is explicitly active -> fall back to the first usable dict.
        values = {"name": [
            {"full_name": "Only Historical", "active_until": "2025-01-01T00:00:00Z"},
        ]}
        self.assertEqual(attio._first(values, "name").get("full_name"), "Only Historical")

    def test_first_skips_non_dict_entries(self):
        values = {"name": [None, "nope", {"full_name": "Real", "active_until": None}]}
        self.assertEqual(attio._first(values, "name").get("full_name"), "Real")

    def test_person_name_parts_returns_active_not_stale(self):
        person = {"values": {"name": [
            {"first_name": "Stale", "last_name": "Doe", "full_name": "Stale Doe",
             "active_until": "2025-01-01T00:00:00Z"},
            {"first_name": "Active", "last_name": "Doe", "full_name": "Active Doe",
             "active_until": None},
        ]}}
        self.assertEqual(attio.person_name_parts(person), ("Active", "Doe", "Active Doe"))

    def test_person_primary_email_returns_active_not_stale(self):
        person = {"values": {"email_addresses": [
            {"email_address": "old@acme.com", "active_until": "2025-01-01T00:00:00Z"},
            {"email_address": "new@acme.com", "active_until": None},
        ]}}
        self.assertEqual(attio.person_primary_email(person), "new@acme.com")


class CrmContextFromRecordsTests(unittest.TestCase):
    def test_usable_with_deal(self):
        with patch.object(attio, "_request", side_effect=fake_request):
            ctx = attio.crm_context_from_records(PERSON, DEAL)
        self.assertTrue(ctx["usable"])
        self.assertEqual(ctx["company"], "Acme Corp")
        self.assertEqual(ctx["deal"], "Acme (Inbound Demo Request)")
        self.assertEqual(ctx["stage"], "Demo Booked")
        self.assertEqual(ctx["email"], "jane@acme.example")
        self.assertIn("Acme Corp", ctx["summary"])
        self.assertIn("stage: Demo Booked", ctx["summary"])
        self.assertIsNone(ctx["owner"])

    def test_person_without_deal_is_not_usable(self):
        ctx = attio.crm_context_from_records(PERSON, None)
        self.assertFalse(ctx["usable"])
        self.assertEqual(ctx["status"], "no_context")


class BuildCrmContextTests(unittest.TestCase):
    def test_happy_path(self):
        with patch.object(attio, "_request", side_effect=fake_request):
            ctx = attio.build_crm_context("+14155201316 John Doe Acme")
        self.assertTrue(ctx["usable"])
        self.assertEqual(ctx["basis"], "attio")
        self.assertEqual(ctx["company"], "Acme Corp")

    def test_empty_query(self):
        ctx = attio.build_crm_context("")
        self.assertFalse(ctx["usable"])
        self.assertEqual(ctx["status"], "empty_query")

    def test_not_found(self):
        with patch.object(attio, "_request", side_effect=lambda *a, **k: {"data": []}):
            ctx = attio.build_crm_context("+14155551234 Nobody")
        self.assertFalse(ctx["usable"])
        self.assertEqual(ctx["status"], "not_found")

    def test_api_error_fails_closed_degraded(self):
        with patch.object(attio, "_request", side_effect=attio.AttioError("http_500")):
            ctx = attio.build_crm_context("+14155551234 Someone")
        self.assertFalse(ctx["usable"])
        self.assertEqual(ctx["status"], "degraded")

    def test_missing_api_key_raises_attio_error(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(attio.AttioError):
                attio._request("GET", "/objects/people/records/x")

    def test_cli_emits_json_and_exits_zero(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with patch.object(attio, "_request", side_effect=fake_request), redirect_stdout(buf):
            rc = attio.main(["+14155201316 John Doe Acme"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["usable"])


class HardeningTests(unittest.TestCase):
    def test_normalize_phone(self):
        self.assertEqual(attio._normalize_phone("+1 (415) 520-1316"), "+14155201316")
        self.assertEqual(attio._normalize_phone("4155201316"), "4155201316")

    def test_clean_strips_control_chars_and_collapses_whitespace(self):
        self.assertEqual(attio._clean("Acme\n\x00 Corp  Inc"), "Acme Corp Inc")
        self.assertIsNone(attio._clean("\x00\n  "))

    def test_text_value_sanitizes(self):
        values = {"name": [{"value": "Evil\nDeal\x07", "attribute_type": "text"}]}
        self.assertEqual(attio._text_value(values, "name"), "Evil Deal")

    def test_malformed_associated_deals_fails_closed(self):
        person = {"id": {"record_id": "p"}, "values": {"associated_deals": [None, "str", {"target_record_id": None}]}}
        with patch.object(attio, "_request", side_effect=lambda m, p, b=None: {"data": [person]}):
            ctx = attio.build_crm_context("+14155201316 Name")
        self.assertFalse(ctx["usable"])  # no crash on non-dict refs

    def test_find_person_by_email_rejects_non_email(self):
        self.assertIsNone(attio.find_person_by_email("not-an-email"))

    def test_main_exits_zero_on_internal_exception(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with patch.object(attio, "build_crm_context", side_effect=RuntimeError("boom")), redirect_stdout(buf):
            rc = attio.main(["anything"])
        self.assertEqual(rc, 0)
        self.assertFalse(json.loads(buf.getvalue())["usable"])

    def test_secret_never_appears_in_output(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with patch.dict("os.environ", {"ATTIO_API_KEY": "SENTINEL-SECRET-123"}), \
             patch.object(attio, "_request", side_effect=attio.AttioError("http_403")), \
             redirect_stdout(buf):
            attio.main(["+14155201316 Name"])
        self.assertNotIn("SENTINEL-SECRET-123", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
