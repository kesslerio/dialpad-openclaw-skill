"""Tests for the S3 customer/prospect segment branching.

When an inbound sales-line SMS resolves an Attio deal at HIGH identity confidence,
the CRM-aware approval-draft copy varies by deal segment (derived from the Attio
`stage` already in crm_context — no new lookups). The segment is also surfaced to
the operator on the Telegram approval card. Segment framing is customer-facing, so
it is applied ONLY at high confidence (same gate as the company name, PR3 PII rule).
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Import webhook_server without leaving scripts/ ahead of bin/ on sys.path, and
# without leaving scripts/send_sms cached in sys.modules. webhook_server does
# `from send_sms import send_sms`, and there are TWO send_sms modules
# (scripts/send_sms.py with the function, bin/send_sms.py the CLI wrapper). The
# send_sms-wrapper tests (test_json_contract, test_send_sms_group_intro) need the
# bin/ version; if we leave scripts/send_sms in sys.modules they'd reuse the wrong
# one. So we snapshot/restore send_sms and drop the scripts/ path entry after import.
_scripts_dir = str(ROOT / "scripts")
_prev_send_sms = sys.modules.get("send_sms")
_added_scripts = _scripts_dir not in sys.path
if _added_scripts:
    sys.path.insert(0, _scripts_dir)

import webhook_server as ws  # noqa: E402
sms_approval = ws.sms_approval

if _added_scripts:
    try:
        sys.path.remove(_scripts_dir)
    except ValueError:
        pass
# Restore send_sms to its prior state so wrapper tests re-resolve their bin/ copy.
if _prev_send_sms is not None:
    sys.modules["send_sms"] = _prev_send_sms
else:
    sys.modules.pop("send_sms", None)


class ClassifyDealSegmentTests(unittest.TestCase):
    def test_customer_stage(self):
        self.assertEqual(ws.classify_deal_segment("Won 🎉"), "customer")

    def test_prospect_demo_stages(self):
        for stage in (
            "Sales Qualified", "DC Scheduling", "Call", "Call - Follow Up Sent",
            "Call - No Show", "Demo Request", "Demo Booked", "Demo - No Show",
            "Demo Canceled", "Demo Completed", "Demo Follow Up", "Negotiations",
        ):
            self.assertEqual(ws.classify_deal_segment(stage), "prospect_demo", stage)

    def test_prospect_cold_stages(self):
        for stage in (
            "MQL", "MQL Sequence", "Manual Review", "Qualifying Sequence",
            "Not Qualified (MQL > SQL)", "Pause - Nurture (Timing)",
        ):
            self.assertEqual(ws.classify_deal_segment(stage), "prospect_cold", stage)

    def test_exact_title_edge_cases_with_spacing(self):
        # Match is case-insensitive with collapsed internal whitespace, so spacing
        # and case variants of the tricky punctuated titles still resolve.
        self.assertEqual(ws.classify_deal_segment("Not Qualified (MQL > SQL)"), "prospect_cold")
        self.assertEqual(ws.classify_deal_segment("not qualified  (mql > sql)"), "prospect_cold")
        self.assertEqual(ws.classify_deal_segment("  Not Qualified (MQL > SQL)  "), "prospect_cold")
        self.assertEqual(ws.classify_deal_segment("Pause - Nurture (Timing)"), "prospect_cold")
        self.assertEqual(ws.classify_deal_segment("pause -  nurture   (timing)"), "prospect_cold")

    def test_generic_stages_are_none(self):
        for stage in ("Lost", "Not a Fit", "", "   "):
            self.assertIsNone(ws.classify_deal_segment(stage), repr(stage))

    def test_unmapped_or_new_stage_is_none(self):
        self.assertIsNone(ws.classify_deal_segment("Brand New Stage 2027"))
        self.assertIsNone(ws.classify_deal_segment("Renamed Demo Stage"))

    def test_robust_to_non_string_and_none(self):
        # Never crash on odd input — a new/renamed/None stage is a safe generic.
        self.assertIsNone(ws.classify_deal_segment(None))
        self.assertIsNone(ws.classify_deal_segment(12345))
        self.assertIsNone(ws.classify_deal_segment(["Won 🎉"]))


class SegmentFramingHighConfidenceTests(unittest.TestCase):
    def _msg(self, stage, confidence="high", company="Acme Corp"):
        crm = {"usable": True, "company": company, "stage": stage}
        ev = {"inbound_context": {"identityConfidence": confidence}}
        return ws._crm_reply_message(ev, {}, crm)

    def test_customer_voice(self):
        msg = self._msg("Won 🎉")
        self.assertIn("great to hear from you again", msg)
        self.assertIn("Acme Corp", msg)

    def test_prospect_demo_voice(self):
        msg = self._msg("Demo Booked")
        self.assertIn("demo conversation", msg)
        self.assertIn("Acme Corp", msg)

    def test_prospect_cold_voice(self):
        msg = self._msg("MQL")
        self.assertIn("thanks for reaching out", msg)
        self.assertIn("Acme Corp", msg)

    def test_generic_voice_for_unmapped_stage(self):
        # Lost / Not a Fit / unmapped → existing generic copy, no special voice.
        for stage in ("Lost", "Not a Fit", "Some New Stage"):
            msg = self._msg(stage)
            self.assertIn("thanks for the update", msg)
            self.assertIn("ShapeScale conversation with Acme Corp", msg)
            self.assertNotIn("great to hear", msg)
            self.assertNotIn("demo conversation", msg)

    def test_copy_reads_safely_even_if_match_wrong(self):
        # No "as a churned customer" framing — copy must be natural if the
        # phone-match resolved the wrong deal.
        for stage in ("Won 🎉", "Demo Booked", "MQL"):
            msg = self._msg(stage)
            for bad in ("churn", "as a customer", "as a prospect", "since you"):
                self.assertNotIn(bad, msg.lower(), f"{bad} leaked for {stage}")

    def test_customer_facing_text_has_no_provenance_tokens(self):
        for stage in ("Won 🎉", "Demo Booked", "MQL", "Lost"):
            msg = self._msg(stage)
            for tok in ("Attio:", "stage:", "QMD", "Segment:", "dealSegment"):
                self.assertNotIn(tok, msg, f"{tok} leaked for {stage}")


class SegmentFramingGateTests(unittest.TestCase):
    """Segment framing is customer-facing → only at high confidence."""

    GENERIC_HIGH_NO_COMPANY = "Hi there, thanks for the update. I have your ShapeScale conversation here and will follow up shortly."

    def _msg(self, confidence, stage="Won 🎉", company="Acme Corp"):
        crm = {"usable": True, "company": company, "stage": stage}
        ev = {"inbound_context": {"identityConfidence": confidence}}
        return ws._crm_reply_message(ev, {}, crm)

    def test_medium_low_none_return_generic_line_unchanged(self):
        for confidence in ("medium", "low", None):
            msg = self._msg(confidence)
            self.assertEqual(msg, self.GENERIC_HIGH_NO_COMPANY, confidence)
            # No segment voice and no company name leak at sub-high confidence.
            self.assertNotIn("great to hear", msg)
            self.assertNotIn("Acme Corp", msg)

    def test_segment_not_set_on_inbound_context_below_high(self):
        for confidence in ("medium", "low", None):
            crm = {"usable": True, "company": "Acme Corp", "stage": "Won 🎉"}
            ev = {"inbound_context": {"identityConfidence": confidence}}
            ws._crm_reply_message(ev, {}, crm)
            self.assertNotIn("dealSegment", ev["inbound_context"], confidence)

    def test_segment_set_on_inbound_context_at_high(self):
        crm = {"usable": True, "company": "Acme Corp", "stage": "Demo Booked"}
        ev = {"inbound_context": {"identityConfidence": "high"}}
        ws._crm_reply_message(ev, {}, crm)
        self.assertEqual(ev["inbound_context"]["dealSegment"], "prospect_demo")

    def test_generic_stage_at_high_does_not_set_segment(self):
        crm = {"usable": True, "company": "Acme Corp", "stage": "Lost"}
        ev = {"inbound_context": {"identityConfidence": "high"}}
        ws._crm_reply_message(ev, {}, crm)
        self.assertNotIn("dealSegment", ev["inbound_context"])


class CompanyEmptyAtHighTests(unittest.TestCase):
    """Company-empty handling must survive the segment additions."""

    def test_empty_company_at_high_still_drops_company_clause(self):
        for stage, marker in (
            ("Won 🎉", "great to hear from you again"),
            ("Demo Booked", "demo conversation"),
            ("MQL", "thanks for reaching out"),
            ("Lost", "thanks for the update"),
        ):
            crm = {"usable": True, "company": "", "stage": stage}
            ev = {"inbound_context": {"identityConfidence": "high"}}
            msg = ws._crm_reply_message(ev, {}, crm)
            self.assertIn(marker, msg, stage)
            # The company clause ("conversation with <company>") must be absent...
            self.assertNotIn("conversation with", msg, stage)
            self.assertNotIn("account with", msg, stage)
            self.assertNotIn("None", msg, stage)

    def test_missing_company_key_does_not_crash(self):
        crm = {"usable": True, "stage": "Won 🎉"}  # no company key at all
        ev = {"inbound_context": {"identityConfidence": "high"}}
        msg = ws._crm_reply_message(ev, {}, crm)
        self.assertIn("great to hear from you again", msg)
        self.assertNotIn(" with ", msg)


class InboundContextBriefSegmentTests(unittest.TestCase):
    def test_brief_renders_segment_when_present(self):
        # Parens are not Telegram MarkdownV1 control chars, so the readable label
        # renders unescaped.
        for segment, label in (
            ("customer", "Customer"),
            ("prospect_demo", "Prospect (demo)"),
            ("prospect_cold", "Prospect (cold)"),
        ):
            brief = ws.build_inbound_context_brief(
                {"identityConfidence": "high", "dealSegment": segment}
            )
            self.assertIn("*Segment:*", brief)
            self.assertIn(label, brief)

    def test_brief_omits_segment_when_absent(self):
        brief = ws.build_inbound_context_brief({"identityConfidence": "high"})
        self.assertNotIn("*Segment:*", brief)

    def test_brief_empty_context_returns_empty(self):
        self.assertEqual(ws.build_inbound_context_brief({}), "")

    def test_brief_renders_set_segment_regardless_of_confidence(self):
        # _crm_reply_message only sets dealSegment at high confidence, but the brief
        # is a dumb renderer: if a segment is present it shows it. This pins the
        # renderer behavior so the gate stays enforced where it belongs (in
        # _crm_reply_message), not silently relocated into the brief.
        brief = ws.build_inbound_context_brief(
            {"identityConfidence": "medium", "dealSegment": "customer"}
        )
        self.assertIn("*Segment:*", brief)
        self.assertIn("Customer", brief)


class FingerprintStabilityTests(unittest.TestCase):
    """create_proactive_reply_draft builds context_fingerprint from a dict that
    includes inbound_context + crm_context (PR2 dedup). Adding dealSegment must not
    make an UNCHANGED draft churn: the same inputs must yield the same fingerprint."""

    def _fingerprint(self, *, segment, stage):
        inbound_context = {"identityConfidence": "high"}
        if segment:
            inbound_context["dealSegment"] = segment
        return sms_approval.build_context_fingerprint(
            {
                "thread_key": "hook:dialpad:sms:+1:+2",
                "sender": "+1",
                "recipient": "+2",
                "message_id": "msg-1",
                "line_display": "Sales",
                "first_contact": None,
                "inbound_context": inbound_context,
                "rich_reply": {"usable": True, "basis": "attio_crm"},
                "crm_context": {"usable": True, "company": "Acme", "stage": stage},
                "calendar_context": None,
            }
        )

    def test_unchanged_draft_has_stable_fingerprint(self):
        a = self._fingerprint(segment="prospect_demo", stage="Demo Booked")
        b = self._fingerprint(segment="prospect_demo", stage="Demo Booked")
        self.assertEqual(a, b)

    def test_segment_change_changes_fingerprint(self):
        # A genuinely different segment is a different draft — dedup should not
        # suppress it.
        demo = self._fingerprint(segment="prospect_demo", stage="Demo Booked")
        customer = self._fingerprint(segment="customer", stage="Won 🎉")
        self.assertNotEqual(demo, customer)

    def _draft_then_fingerprint(self, stage):
        """Mirror create_proactive_reply_draft's ordering: build the message first
        (which mutates inbound_context['dealSegment'] via _crm_reply_message), THEN
        fingerprint the mutated inbound_context. A reorder that fingerprints before
        the mutation would silently drop the segment and is what this pins."""
        event = {"inbound_context": {"identityConfidence": "high"}}
        crm = {"usable": True, "company": "Acme", "stage": stage}
        ws._crm_reply_message(event, {}, crm)
        return sms_approval.build_context_fingerprint(
            {
                "inbound_context": event["inbound_context"],
                "crm_context": crm,
            }
        )

    def test_mutated_segment_is_folded_into_fingerprint(self):
        # The high-confidence Attio match sets dealSegment, and the fingerprint must
        # reflect it (so a different segment is a different draft) while staying
        # stable for an unchanged draft.
        a = self._draft_then_fingerprint("Demo Booked")
        b = self._draft_then_fingerprint("Demo Booked")
        self.assertEqual(a, b)  # unchanged draft → stable fingerprint
        customer = self._draft_then_fingerprint("Won 🎉")
        self.assertNotEqual(a, customer)  # different segment → different fingerprint


class NoNewSendCallerTests(unittest.TestCase):
    def test_no_caller_of_send_proactive_reply(self):
        # S3 is draft-copy only: it must not introduce an unattended send path.
        import inspect
        src = inspect.getsource(ws)
        send_callers = [
            line.strip() for line in src.splitlines()
            if "send_proactive_reply(" in line
            and "should_send_proactive_reply(" not in line
            and not line.lstrip().startswith("def send_proactive_reply(")
        ]
        self.assertEqual(send_callers, [], f"new send_proactive_reply caller: {send_callers}")

    def test_approve_draft_caller_count_unchanged(self):
        # The only legitimate approve_draft caller is the operator-driven Telegram
        # callback handler (a human pressing approve). S3 must not add another.
        import inspect
        src = inspect.getsource(ws)
        approve_callers = [
            line.strip() for line in src.splitlines()
            if "sms_approval.approve_draft(" in line
        ]
        self.assertEqual(len(approve_callers), 1, f"unexpected approve_draft callers: {approve_callers}")


if __name__ == "__main__":
    unittest.main()
