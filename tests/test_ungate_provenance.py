"""Tests for the U7 un-gate + provenance (PR3).

The CRM/calendar enrichment now runs in the operator-approval draft lane at any
identity confidence (auto-send does not exist, so this is safe), and each draft
carries an operator-facing provenance line — never in the customer-facing text.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import webhook_server as ws  # noqa: E402


class GateRelaxationTests(unittest.TestCase):
    def test_allows_sms_at_any_confidence(self):
        for conf in ("low", "medium", "high", None):
            ev = {"event_type": "sms", "inbound_context": {"identityConfidence": conf}}
            self.assertTrue(ws._sales_context_draft_allowed(ev), conf)

    def test_allows_sms_with_no_inbound_context(self):
        self.assertTrue(ws._sales_context_draft_allowed({"event_type": "sms"}))
        self.assertTrue(ws._sales_context_draft_allowed({}))  # defaults to sms

    def test_rejects_non_sms(self):
        self.assertFalse(ws._sales_context_draft_allowed({"event_type": "missed_call"}))


class UngatedLookupTests(unittest.TestCase):
    def test_crm_lookup_runs_at_low_confidence(self):
        # Before U7 this returned {"usable": False, "status": "not_allowed"}.
        event = {
            "event_type": "sms",
            "sender_number": "+14155550123",
            "inbound_context": {"identityConfidence": "low"},
        }
        crm_payload = {"usable": True, "basis": "attio", "summary": "Acme Corp Demo Booked",
                       "company": "Acme Corp", "stage": "Demo Booked", "deal": "Acme", "owner": None}
        with patch.object(ws, "_run_context_command", return_value=crm_payload):
            ctx = ws.lookup_sales_crm_context(event, sender_enrichment={"contact_name": "Jane", "company": "Acme"})
        self.assertTrue(ctx["usable"])
        self.assertEqual(ctx["company"], "Acme Corp")


class ProvenanceTests(unittest.TestCase):
    def test_attio_provenance(self):
        ev = {"crm_context": {"usable": True, "company": "Acme Corp", "stage": "Demo Booked"}}
        self.assertEqual(ws._build_draft_provenance(ev), "Attio: Acme Corp · stage: Demo Booked")

    def test_attio_matched_without_detail(self):
        ev = {"crm_context": {"usable": True}}
        self.assertEqual(ws._build_draft_provenance(ev), "Attio: matched")

    def test_qmd_provenance(self):
        ev = {"rich_reply": {"usable": True, "basis": "knowledge_backed"}}
        self.assertEqual(ws._build_draft_provenance(ev), "QMD knowledge")

    def test_calendar_and_combined(self):
        ev = {
            "crm_context": {"usable": True, "company": "Acme"},
            "calendar_context": {"usable": True, "summary": "Upcoming demo: Acme"},
        }
        prov = ws._build_draft_provenance(ev)
        self.assertIn("Attio: Acme", prov)
        self.assertIn("Calendar: Upcoming demo: Acme", prov)

    def test_none_when_nothing_usable(self):
        self.assertIsNone(ws._build_draft_provenance({}))
        self.assertIsNone(ws._build_draft_provenance({"crm_context": {"usable": False}}))

    def test_crm_aware_rich_reply_not_double_counted_as_qmd(self):
        # an attio_crm/calendar rich_reply is already reflected by crm/calendar
        # context; it must not also show as "QMD knowledge".
        ev = {"rich_reply": {"usable": True, "basis": "attio_crm"}}
        self.assertIsNone(ws._build_draft_provenance(ev))


if __name__ == "__main__":
    unittest.main()
