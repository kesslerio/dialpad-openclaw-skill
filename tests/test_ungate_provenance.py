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

    def test_allows_missed_call(self):
        self.assertTrue(ws._sales_context_draft_allowed({"event_type": "missed_call"}))
        self.assertFalse(ws._sales_context_draft_allowed({"event_type": "voicemail"}))


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

    def test_missed_call_crm_context_builds_call_specific_rich_reply(self):
        event = {
            "event_type": "missed_call",
            "sender_number": "+14155550123",
            "text": "",
            "inbound_context": {"identityConfidence": "high"},
        }
        crm_payload = {
            "usable": True,
            "basis": "attio",
            "summary": "Acme Corp Manual Review",
            "company": "Acme Corp",
            "stage": "Manual Review",
            "deal": "Acme",
            "owner": None,
        }
        with patch.object(ws, "_run_context_command", side_effect=[crm_payload]):
            rich = ws.build_rich_sms_reply(event, sender_enrichment={"first_name": "Jane"})
        self.assertTrue(rich["usable"])
        self.assertEqual(rich["basis"], "attio_crm")
        self.assertIn("sorry we missed your call", rich["message"])
        self.assertNotIn("texted", rich["message"])

    def test_missed_call_calendar_miss_preserves_crm_draft(self):
        event = {
            "event_type": "missed_call",
            "sender_number": "+14155550123",
            "text": "",
            "inbound_context": {
                "identityConfidence": "high",
                "contextDraftAllowed": True,
            },
        }
        crm_payload = {
            "usable": True,
            "basis": "attio",
            "summary": "Acme Corp Demo Booked",
            "company": "Acme Corp",
            "stage": "Demo Booked",
            "deal": "Acme",
            "owner": None,
        }
        calendar_payload = {"usable": False, "status": "not_found"}
        with patch.object(ws, "_run_context_command", side_effect=[crm_payload, calendar_payload]):
            rich = ws.build_rich_sms_reply(event, sender_enrichment={"first_name": "Jane"})
        self.assertTrue(rich["usable"])
        self.assertEqual(rich["basis"], "attio_crm")
        self.assertEqual(event["calendar_context"]["status"], "not_found")


class ProvenanceTests(unittest.TestCase):
    def test_attio_provenance(self):
        ev = {"crm_context": {"usable": True, "company": "Acme Corp", "stage": "Demo Booked"}}
        self.assertEqual(ws._build_draft_provenance(ev), "Attio: Acme Corp · stage: Demo Booked")

    def test_attio_matched_without_detail(self):
        ev = {"crm_context": {"usable": True}}
        self.assertEqual(ws._build_draft_provenance(ev), "Attio: matched")

    def test_qmd_provenance(self):
        ev = {"rich_reply": {"usable": True, "basis": "shapescale_knowledge"}}
        self.assertEqual(ws._build_draft_provenance(ev), "QMD knowledge")

    def test_model_qmd_provenance(self):
        ev = {"text": "How does it work?", "rich_reply": {"usable": True, "basis": "model_shapescale_knowledge"}}
        self.assertEqual(ws._build_draft_provenance(ev), "QMD knowledge")
        statuses = ws.collect_enrichment_source_statuses(ev)
        self.assertEqual(statuses["qmd"]["status"], "usable")
        self.assertEqual(statuses["qmd"]["basis"], "shapescale_knowledge")

    def test_recent_thread_link_not_labeled_qmd(self):
        # link-resend from prior SMS history must not claim a QMD source
        ev = {"rich_reply": {"usable": True, "basis": "recent_thread_link"}}
        self.assertEqual(ws._build_draft_provenance(ev), "Prior-thread link")

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

    def test_source_statuses_show_qmd_not_applicable_for_silent_call(self):
        ev = {
            "event_type": "missed_call",
            "crm_context": {"usable": False, "status": "not_found"},
            "calendar_context": {"usable": False, "status": "not_applicable"},
            "rich_reply": {"usable": False, "status": "not_answerable"},
            "text": "",
        }
        statuses = ws.collect_enrichment_source_statuses(ev)
        self.assertEqual(statuses["crm"]["status"], "not_found")
        self.assertEqual(statuses["calendar"]["status"], "not_applicable")
        self.assertEqual(statuses["comms"]["status"], "not_applicable")
        self.assertEqual(statuses["qmd"]["status"], "not_applicable")

    def test_source_statuses_do_not_copy_crm_reply_status_into_qmd(self):
        ev = {
            "event_type": "missed_call",
            "crm_context": {"usable": True, "status": "ok", "basis": "attio"},
            "rich_reply": {"usable": True, "status": "ok", "basis": "attio_crm"},
            "text": "pricing question from transcript",
        }
        statuses = ws.collect_enrichment_source_statuses(ev)
        self.assertEqual(statuses["crm"]["status"], "usable")
        self.assertEqual(statuses["comms"]["status"], "not_applicable")
        self.assertEqual(statuses["qmd"]["status"], "not_applicable")

    def test_source_statuses_show_crm_not_applicable_when_qmd_skipped_crm(self):
        ev = {
            "event_type": "sms",
            "rich_reply": {"usable": True, "status": "ok", "basis": "shapescale_knowledge"},
            "text": "how do I reset the app?",
        }
        with patch.object(ws, "DIALPAD_CRM_CONTEXT_COMMAND", "crm-lookup"):
            statuses = ws.collect_enrichment_source_statuses(ev)
        self.assertEqual(statuses["crm"]["status"], "not_applicable")
        self.assertEqual(statuses["comms"]["status"], "not_applicable")
        self.assertEqual(statuses["qmd"]["status"], "usable")


class CustomerTextSafetyTests(unittest.TestCase):
    CRM = {"usable": True, "company": "Acme Corp", "stage": "Demo Booked"}

    def _msg(self, confidence):
        ev = {"inbound_context": {"identityConfidence": confidence}}
        return ws._crm_reply_message(ev, {}, self.CRM)

    def test_company_named_only_at_high_confidence(self):
        self.assertIn("Acme Corp", self._msg("high"))
        self.assertNotIn("Acme Corp", self._msg("medium"))
        self.assertNotIn("Acme Corp", self._msg("low"))
        self.assertNotIn("Acme Corp", self._msg(None))

    def test_customer_text_never_contains_provenance_tokens(self):
        # the operator-facing provenance tokens must never reach customer-facing text
        for conf in ("high", "medium", "low"):
            msg = self._msg(conf)
            for tok in ("Attio:", "stage:", "QMD", "Calendar:", "↳"):
                self.assertNotIn(tok, msg, f"{tok} leaked at {conf}")

    def test_low_confidence_draft_is_safe_generic_crm_line(self):
        msg = self._msg("low")
        self.assertIn("ShapeScale conversation here", msg)  # company-free

    def test_greeting_suppresses_name_at_low_confidence(self):
        crm = {"usable": True, "company": "Acme"}
        low = ws._crm_reply_message({"inbound_context": {"identityConfidence": "low"}},
                                    {"first_name": "Wrong"}, crm)
        self.assertIn("Hi there,", low)
        self.assertNotIn("Wrong", low)
        # medium/high keep the (known-contact) name
        med = ws._crm_reply_message({"inbound_context": {"identityConfidence": "medium"}},
                                    {"first_name": "Jane"}, crm)
        self.assertIn("Jane", med)

    def test_meeting_greeting_suppresses_name_at_low_confidence(self):
        ev = {"inbound_context": {"identityConfidence": "low"}, "text": "running late"}
        msg = ws._meeting_reply_message(ev, {"first_name": "Wrong"}, {}, {})
        self.assertIn("Hi there,", msg)
        self.assertNotIn("Wrong", msg)

    def test_low_confidence_missed_call_crm_copy_is_call_specific_and_pii_safe(self):
        ev = {"event_type": "missed_call", "inbound_context": {"identityConfidence": "low"}}
        msg = ws._crm_reply_message(ev, {"first_name": "Wrong"}, {"usable": True, "company": "Acme"})
        self.assertIn("sorry we missed your call", msg)
        self.assertIn("Hi there,", msg)
        self.assertNotIn("Wrong", msg)
        self.assertNotIn("Acme", msg)


class CalendarUngateTests(unittest.TestCase):
    def test_calendar_lookup_runs_at_low_confidence(self):
        event = {"event_type": "sms", "sender_number": "+14155550123", "text": "running late",
                 "inbound_context": {"identityConfidence": "low"}}
        cal_payload = {"usable": True, "basis": "attio", "summary": "Upcoming demo: Acme", "startsInMinutes": 30}
        with patch.object(ws, "_run_context_command", return_value=cal_payload):
            ctx = ws.lookup_sales_calendar_context(
                event, crm_context={"company": "Acme"}, sender_enrichment={"contact_name": "Jane"})
        self.assertTrue(ctx["usable"])

    def test_missed_call_calendar_lookup_does_not_require_sms_text_intent(self):
        event = {
            "event_type": "missed_call",
            "sender_number": "+14155550123",
            "timestamp": 1760000000000,
            "inbound_context": {"identityConfidence": "low", "contextDraftAllowed": True},
        }
        cal_payload = {
            "usable": True,
            "basis": "attio",
            "summary": "Recent demo: Acme",
            "startsInMinutes": 30,
            "demoState": "recent",
        }
        with patch.object(ws, "_run_context_command", return_value=cal_payload):
            ctx = ws.lookup_sales_calendar_context(
                event,
                crm_context={"usable": True, "company": "Acme", "deal": "Acme"},
                sender_enrichment={"contact_name": "Jane"},
            )
        self.assertTrue(ctx["usable"])
        self.assertIn("Recent demo", ctx["summary"])

    def test_missed_call_calendar_lookup_uses_crm_demo_context(self):
        event = {
            "event_type": "missed_call",
            "sender_number": "+14155550123",
            "timestamp": 1760000000000,
            "inbound_context": {"identityConfidence": "medium", "contextDraftAllowed": False},
        }
        cal_payload = {
            "usable": True,
            "basis": "attio",
            "summary": "Upcoming demo: Acme",
            "startsInMinutes": 30,
            "demoState": "upcoming",
        }
        crm_context = {"usable": True, "company": "Acme", "deal": "Acme demo", "stage": "Demo Booked"}
        with patch.object(ws, "_run_context_command", return_value=cal_payload) as run:
            ctx = ws.lookup_sales_calendar_context(event, crm_context=crm_context)
        self.assertTrue(ctx["usable"])
        self.assertTrue(run.called)

    def test_missed_call_knowledge_reply_is_call_specific(self):
        event = {
            "event_type": "missed_call",
            "sender_number": "+14155550123",
            "recipient_number": "+14155201316",
            "text": "What does it cost?",
        }
        with patch.object(ws, "lookup_recent_sms_thread", return_value=[]), \
                patch.object(ws, "lookup_shapescale_knowledge", return_value={
                    "usable": True,
                    "status": "ok",
                    "text": "ShapeScale costs $1,799 upfront.",
                }):
            rich = ws.build_rich_sms_reply(event)
        self.assertTrue(rich["usable"])
        self.assertEqual(rich["basis"], "shapescale_knowledge")
        self.assertIn("sorry we missed your call", rich["message"])

    def test_missed_call_transcript_question_uses_qmd_before_crm_fallback(self):
        event = {
            "event_type": "missed_call",
            "sender_number": "+14155550123",
            "recipient_number": "+14155201316",
            "text": "What does it cost?",
        }
        with patch.object(ws, "lookup_recent_sms_thread", return_value=[]), \
                patch.object(ws, "lookup_sales_crm_context", return_value={
                    "usable": True,
                    "status": "ok",
                    "basis": "attio",
                    "company": "Acme",
                    "stage": "Manual Review",
                }), \
                patch.object(ws, "lookup_shapescale_knowledge", return_value={
                    "usable": True,
                    "status": "ok",
                    "text": "ShapeScale costs $1,799 upfront.",
                }):
            rich = ws.build_rich_sms_reply(event)
        self.assertTrue(rich["usable"])
        self.assertEqual(rich["basis"], "shapescale_knowledge")

    def test_sms_meeting_logistics_rejects_recent_demo_context(self):
        event = {
            "event_type": "sms",
            "sender_number": "+14155550123",
            "recipient_number": "+14155201316",
            "text": "I'm running late",
        }
        event["crm_context"] = {"usable": True, "status": "ok", "basis": "attio", "company": "Acme"}
        event["calendar_context"] = {
            "usable": True,
            "status": "ok",
            "basis": "attio",
            "summary": "Recent demo: Acme",
            "demoState": "recent",
        }
        rich = ws.build_contextual_sales_sms_reply(event)
        self.assertFalse(rich["usable"])
        self.assertEqual(rich["status"], "calendar_recent")


class ProvenanceRobustnessTests(unittest.TestCase):
    def test_non_string_company_does_not_raise(self):
        prov = ws._build_draft_provenance({"crm_context": {"usable": True, "company": 12345, "stage": 99}})
        self.assertIn("12345", prov)

    def test_basisless_rich_reply_not_labeled_qmd(self):
        self.assertIsNone(ws._build_draft_provenance({"rich_reply": {"usable": True}}))


class NoUnattendedSendInvariantTests(unittest.TestCase):
    def test_send_proactive_reply_has_no_callers(self):
        # U7's safety basis: enrichment feeds operator-approval DRAFTS only; there is
        # no unattended auto-send. If a real caller of send_proactive_reply appears
        # (e.g. S4) without re-gating the customer-facing enrichment, fail loudly.
        import inspect
        src = inspect.getsource(ws)
        callers = [
            line.strip() for line in src.splitlines()
            if "send_proactive_reply(" in line
            and "should_send_proactive_reply(" not in line
            and not line.lstrip().startswith("def send_proactive_reply(")
        ]
        self.assertEqual(callers, [], f"send_proactive_reply gained a caller; re-gate U7 first: {callers}")


if __name__ == "__main__":
    unittest.main()
