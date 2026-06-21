"""Tests for the S4 confidence-gated auto-send risk classifier — SHADOW MODE ONLY.

S4 *computes and logs* an auto-send decision for each eligible drafted reply so S6
can later evaluate it; it NEVER sends anything. The hard invariant is that no
automated / non-human code path feeds a shadow-derived value (autoSendShadow /
wouldAutoSend) into approve_draft or any send_func — the shadow decision is never
wired to a send. approve_draft itself is legitimately called by the human-approval
flow with the real sender as send_func; that is the human-gated lane, not S4.
"""
import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# Importing webhook_server pulls in scripts/send_sms.py under the bare name
# "send_sms". The bin/ wrapper tests (test_json_contract, test_send_sms_group_intro)
# import a *different* module that also calls itself "send_sms" (bin/send_sms.py),
# resolving via their own sys.path. Whichever loads first wins the sys.modules
# cache. Snapshot the pre-import state and restore it so this file — which sorts
# first alphabetically — does not leak scripts/send_sms into those bin/ tests.
_send_sms_before = sys.modules.get("send_sms")

import webhook_server as ws  # noqa: E402
import sms_approval as sa  # noqa: E402

if _send_sms_before is None:
    sys.modules.pop("send_sms", None)
else:
    sys.modules["send_sms"] = _send_sms_before


def _event(category=None, confidence=None, *, rich_usable=True, inbound_context=True):
    """Build a normalized_event slice the shadow classifier reads."""
    ev = {"event_type": "sms"}
    if category is not None or rich_usable:
        ev["rich_reply"] = {"usable": rich_usable, "category": category}
    if inbound_context:
        ev["inbound_context"] = {"identityConfidence": confidence}
    return ev


class AllowlistDecisionTests(unittest.TestCase):
    def test_link_issue_high_confidence_would_auto_send(self):
        decision = ws.evaluate_auto_send_shadow(_event("link_issue", "high"), {"state": "normal"})
        self.assertTrue(decision["wouldAutoSend"])
        self.assertEqual(decision["category"], "link_issue")
        self.assertEqual(decision["mode"], "shadow")

    def test_non_allowlisted_categories_never_auto_send(self):
        # pricing/product/booking/None are real classifier categories; none are
        # auto-send-eligible while the allowlist is link_issue-only.
        for category in ("pricing", "product", "booking", None):
            decision = ws.evaluate_auto_send_shadow(_event(category, "high"))
            self.assertFalse(
                decision["wouldAutoSend"], f"{category} must not auto-send",
            )
            self.assertFalse(decision["allowlistMatch"], category)

    def test_only_link_issue_in_allowlist(self):
        self.assertEqual(ws.DIALPAD_AUTO_SEND_SHADOW_ALLOWLIST, frozenset({"link_issue"}))


class ConfidenceGateTests(unittest.TestCase):
    def test_link_issue_low_or_medium_confidence_does_not_auto_send(self):
        for confidence in ("low", "medium", None):
            decision = ws.evaluate_auto_send_shadow(_event("link_issue", confidence))
            self.assertFalse(
                decision["wouldAutoSend"], f"confidence={confidence} must gate",
            )
            self.assertFalse(decision["confidenceMet"], confidence)

    def test_allowlisted_but_rich_reply_not_usable_does_not_auto_send(self):
        ev = _event("link_issue", "high", rich_usable=False)
        decision = ws.evaluate_auto_send_shadow(ev)
        self.assertFalse(decision["wouldAutoSend"])
        self.assertFalse(decision["richReplyUsable"])


class VoicemailMissedCallSiteTests(unittest.TestCase):
    def test_no_inbound_context_and_no_rich_reply_does_not_crash(self):
        # The voicemail/missed-call site builds normalized_event WITHOUT
        # inbound_context, and build_rich_sms_reply rejects voicemail
        # (unsupported_event) so rich_reply is absent. Decision = not eligible.
        ev = {"event_type": "voicemail"}
        decision = ws.evaluate_auto_send_shadow(ev)
        self.assertFalse(decision["wouldAutoSend"])
        self.assertIsNone(decision["category"])
        self.assertIsNone(decision["identityConfidence"])

    def test_unusable_rich_reply_without_inbound_context(self):
        ev = {"event_type": "voicemail", "rich_reply": {"usable": False, "status": "unsupported_event"}}
        decision = ws.evaluate_auto_send_shadow(ev)
        self.assertFalse(decision["wouldAutoSend"])

    def test_log_helper_no_op_when_decision_absent(self):
        # not-eligible / opt-out / voicemail-with-no-draft paths never stamp the
        # decision; logging must be a silent no-op, never a crash.
        ws.log_auto_send_shadow({"event_type": "voicemail"})  # no autoSendShadow key


class RichDraftCategoryFallbackTests(unittest.TestCase):
    def test_reads_category_from_inbound_context_when_rich_reply_lacks_it(self):
        # create_proactive_reply_draft stamps richDraftCategory onto inbound_context;
        # the shadow classifier falls back to it when rich_reply has no 'category'.
        ev = {
            "event_type": "sms",
            "rich_reply": {"usable": True},  # no 'category' key
            "inbound_context": {"identityConfidence": "high", "richDraftCategory": "link_issue"},
        }
        decision = ws.evaluate_auto_send_shadow(ev, {"state": "normal"})
        self.assertTrue(decision["wouldAutoSend"])
        self.assertEqual(decision["category"], "link_issue")


class DraftStampingIntegrationTests(unittest.TestCase):
    """Pin the S6 contract: create_proactive_reply_draft stamps the shadow decision
    onto normalized_event AND persists it into draft metadata, but only on the
    draft-creation path (never before the eligibility gate)."""

    def _drive_create_draft(self, normalized_event):
        captured = {}

        class _FakeConn:
            def close(self):
                pass

        def _fake_create_replacement_draft(conn, **kwargs):
            captured["metadata"] = kwargs.get("metadata")
            return {"draft_id": "draft-test"}

        with patch.object(ws, "should_send_proactive_reply", return_value=True), \
                patch.object(ws, "build_proactive_reply_message", return_value="hi"), \
                patch.object(ws, "classify_sms_reply_policy", return_value={"state": "normal"}), \
                patch.object(ws.sms_approval, "init_db", return_value=_FakeConn()), \
                patch.object(ws.sms_approval, "is_opted_out", return_value=False), \
                patch.object(ws.sms_approval, "build_context_fingerprint", return_value="fp"), \
                patch.object(ws.sms_approval, "create_replacement_draft",
                             side_effect=_fake_create_replacement_draft):
            created, status, *_ = ws.create_proactive_reply_draft(normalized_event)
        return created, status, captured.get("metadata")

    def test_link_issue_high_confidence_stamps_metadata_and_event(self):
        ev = {
            "event_type": "sms",
            "sender_number": "+14155550100",
            "recipient_number": "+14155201316",
            "rich_reply": {"usable": True, "category": "link_issue"},
            "inbound_context": {"identityConfidence": "high"},
        }
        created, status, metadata = self._drive_create_draft(ev)
        self.assertTrue(created, status)
        # event carries the decision
        self.assertTrue(ev["autoSendShadow"]["wouldAutoSend"])
        # metadata (what S6 reads) carries the same decision
        self.assertIsInstance(metadata, dict)
        self.assertIn("autoSendShadow", metadata)
        self.assertTrue(metadata["autoSendShadow"]["wouldAutoSend"])

    def test_not_eligible_path_does_not_stamp_shadow(self):
        # When the eligibility gate rejects, no draft is built and no shadow is
        # stamped — this protects the "shadow only fires on drafted events" contract.
        ev = {"event_type": "sms", "sender_number": "+1", "recipient_number": "+2"}
        with patch.object(ws, "should_send_proactive_reply", return_value=False), \
                patch.object(ws, "invalidate_pending_sms_drafts", return_value=True), \
                patch.object(ws, "classify_sms_reply_policy", return_value={"state": "normal"}):
            created, status, *_ = ws.create_proactive_reply_draft(ev)
        self.assertFalse(created)
        self.assertNotIn("autoSendShadow", ev)

    def test_inbound_context_present_but_rich_reply_none_does_not_crash(self):
        # Real production state: inbound_context resolved but classify returned no
        # category, so rich_reply is absent. Decision must be not-eligible, no crash.
        ev = {
            "event_type": "sms",
            "sender_number": "+14155550100",
            "recipient_number": "+14155201316",
            "inbound_context": {"identityConfidence": "high"},  # no rich_reply key
        }
        created, status, metadata = self._drive_create_draft(ev)
        self.assertTrue(created, status)
        self.assertFalse(ev["autoSendShadow"]["wouldAutoSend"])
        self.assertFalse(metadata["autoSendShadow"]["richReplyUsable"])


class ConfidenceSignalsNoPiiTests(unittest.TestCase):
    PII = ("Acme Corp", "Jane Doe", "+14155550123", "jane@example.com")

    def test_decision_contains_only_booleans_and_enums_no_pii(self):
        # Feed a fully-enriched event that *has* PII alongside the rich reply, and
        # assert the shadow decision surfaces none of it.
        ev = {
            "event_type": "sms",
            "sender": "Jane Doe",
            "sender_number": "+14155550123",
            "rich_reply": {"usable": True, "category": "link_issue",
                           "message": "Hi Jane Doe, try this link: https://bysha.pe/x"},
            "inbound_context": {"identityConfidence": "high", "contactName": "Jane Doe"},
            "crm_context": {"usable": True, "company": "Acme Corp"},
        }
        decision = ws.evaluate_auto_send_shadow(ev)
        flat = repr(decision)
        for token in self.PII:
            self.assertNotIn(token, flat, f"PII {token!r} leaked into shadow decision")
        # values are only booleans, the category/confidence enums, or "shadow"
        allowed_scalars = {True, False, None, "shadow", "link_issue", "high"}
        for value in decision.values():
            self.assertIn(value, allowed_scalars, f"unexpected scalar {value!r}")


class NoAutomatedPathIntoSendInvariantTests(unittest.TestCase):
    """The corrected S4 invariant: the shadow decision is never wired to a send.

    approve_draft IS legitimately called by the human-approval flow with the real
    sender as send_func default, so the invariant is NOT 'approve_draft is never
    called'. Instead: no AUTOMATED / non-human code path may feed a shadow-derived
    value (autoSendShadow / wouldAutoSend) into approve_draft or any send_func.
    We scan BOTH webhook_server.py and sms_approval.py.
    """

    SHADOW_TOKENS = ("autoSendShadow", "wouldAutoSend", "evaluate_auto_send_shadow")
    SEND_SINKS = ("approve_draft(", "send_func", "dialpad_send_sms(",
                  "send_proactive_reply(", "send_sms(")

    def _source_lines(self):
        return (
            inspect.getsource(ws).splitlines()
            + inspect.getsource(sa).splitlines()
        )

    def test_no_shadow_token_on_any_send_sink_line(self):
        # A shadow token and a send sink must never co-occur on one line — that
        # would be the shape of wiring a shadow decision into a send.
        offenders = []
        for line in self._source_lines():
            has_shadow = any(tok in line for tok in self.SHADOW_TOKENS)
            if not has_shadow:
                continue
            # 'should_send_proactive_reply(' is an eligibility check, not a send.
            sink_hit = any(
                sink in line
                and not (sink == "send_proactive_reply(" and "should_send_proactive_reply(" in line)
                for sink in self.SEND_SINKS
            )
            if sink_hit:
                offenders.append(line.strip())
        self.assertEqual(
            offenders, [],
            f"shadow decision appears on a send-sink line (auto-send wiring): {offenders}",
        )

    def test_shadow_classifier_does_not_call_any_sender(self):
        # The classifier and its logger must not reference any send sink in their
        # executable code. Strip the docstring first — the docstring intentionally
        # names the sinks to document the invariant, which is not a call.
        import ast
        for fn in (ws.evaluate_auto_send_shadow, ws.log_auto_send_shadow):
            tree = ast.parse(inspect.getsource(fn).lstrip())
            fn_node = tree.body[0]
            if (fn_node.body and isinstance(fn_node.body[0], ast.Expr)
                    and isinstance(fn_node.body[0].value, ast.Constant)):
                fn_node.body = fn_node.body[1:]  # drop docstring
            code = ast.unparse(fn_node)
            for sink in ("approve_draft", "send_func", "dialpad_send_sms",
                         "send_proactive_reply", "send_sms"):
                self.assertNotIn(sink, code, f"{fn.__name__} references send sink {sink}")

    def test_approve_draft_only_human_caller_in_webhook_server(self):
        # The one approve_draft call in webhook_server.py is the Telegram human
        # callback: it derives actor_id from the human 'from' field and never from
        # a shadow value. Assert no approve_draft call site mentions a shadow token.
        src_lines = inspect.getsource(ws).splitlines()
        for idx, line in enumerate(src_lines):
            if "approve_draft(" not in line:
                continue
            # inspect a small window around the call for shadow tokens
            window = " ".join(src_lines[max(0, idx - 2): idx + 12])
            for tok in self.SHADOW_TOKENS:
                self.assertNotIn(
                    tok, window,
                    f"approve_draft call near line {idx} references shadow token {tok}",
                )

    def test_send_proactive_reply_stays_callerless(self):
        # Keep the existing NoUnattendedSendInvariantTests guarantee green: S4 must
        # not resurrect send_proactive_reply as a real caller.
        src = inspect.getsource(ws)
        callers = [
            line.strip() for line in src.splitlines()
            if "send_proactive_reply(" in line
            and "should_send_proactive_reply(" not in line
            and not line.lstrip().startswith("def send_proactive_reply(")
        ]
        self.assertEqual(callers, [], f"send_proactive_reply gained a caller: {callers}")


class ReplyPolicyGateTests(unittest.TestCase):
    def test_risky_policy_blocks_auto_send(self):
        # A 'risky' reply needs two-step human confirmation -> never auto-send-eligible,
        # even for an allowlisted high-confidence link_issue.
        decision = ws.evaluate_auto_send_shadow(_event("link_issue", "high"), {"state": "risky"})
        self.assertFalse(decision["wouldAutoSend"])
        self.assertFalse(decision["policyNormal"])

    def test_absent_policy_fails_closed(self):
        decision = ws.evaluate_auto_send_shadow(_event("link_issue", "high"), None)
        self.assertFalse(decision["wouldAutoSend"])


class PersistedOptOutShadowTests(unittest.TestCase):
    def test_persisted_opt_out_does_not_stamp_shadow(self):
        # A previously opted-out customer (persisted is_opted_out=True) is blocked; the
        # shadow must NOT be stamped, so S6 never sees an opted-out customer as eligible.
        ev = {
            "event_type": "sms",
            "sender_number": "+14155550100",
            "recipient_number": "+14155201316",
            "rich_reply": {"usable": True, "category": "link_issue"},
            "inbound_context": {"identityConfidence": "high"},
        }

        class _FakeConn:
            def close(self):
                pass

        with patch.object(ws, "should_send_proactive_reply", return_value=True), \
                patch.object(ws, "build_proactive_reply_message", return_value="hi"), \
                patch.object(ws, "classify_sms_reply_policy", return_value={"state": "normal"}), \
                patch.object(ws.sms_approval, "init_db", return_value=_FakeConn()), \
                patch.object(ws.sms_approval, "is_opted_out", return_value=True), \
                patch.object(ws.sms_approval, "build_context_fingerprint", return_value="fp"):
            created, status, *_ = ws.create_proactive_reply_draft(ev)
        self.assertFalse(created)
        self.assertEqual(status, "blocked_opt_out")
        self.assertNotIn("autoSendShadow", ev)


class DraftPersistenceFailureShadowTests(unittest.TestCase):
    def test_failed_persistence_does_not_stamp_shadow(self):
        # If create_replacement_draft raises, the handler returns
        # approval_persistence_failed and the event must NOT be left stamped, so a
        # failed persistence cannot pollute S6 with a phantom auto-send decision.
        ev = {
            "event_type": "sms",
            "sender_number": "+14155550100",
            "recipient_number": "+14155201316",
            "rich_reply": {"usable": True, "category": "link_issue"},
            "inbound_context": {"identityConfidence": "high"},
        }

        class _FakeConn:
            def close(self):
                pass

        with patch.object(ws, "should_send_proactive_reply", return_value=True), \
                patch.object(ws, "build_proactive_reply_message", return_value="hi"), \
                patch.object(ws, "classify_sms_reply_policy", return_value={"state": "normal"}), \
                patch.object(ws.sms_approval, "init_db", return_value=_FakeConn()), \
                patch.object(ws.sms_approval, "is_opted_out", return_value=False), \
                patch.object(ws.sms_approval, "build_context_fingerprint", return_value="fp"), \
                patch.object(ws.sms_approval, "create_replacement_draft",
                             side_effect=RuntimeError("db down")):
            created, status, *_ = ws.create_proactive_reply_draft(ev)
        self.assertFalse(created)
        self.assertEqual(status, "approval_persistence_failed")
        self.assertNotIn("autoSendShadow", ev)


class HookPayloadShadowContainmentTests(unittest.TestCase):
    def test_hook_payload_never_carries_shadow(self):
        # All 3 call sites converge at build_openclaw_hook_payload; the shadow
        # decision must never reach the outbound OpenClaw hook, even when stamped on
        # the event. auto_reply (forwarded to the hook) must carry no shadow key.
        import json as _json
        ev = {
            "event_type": "sms",
            "sender_number": "+14155550100",
            "recipient_number": "+14155201316",
            "autoSendShadow": {"mode": "shadow", "wouldAutoSend": True},
            "auto_reply": {"status": "draft_created", "draftCreated": True,
                           "replyPolicy": {"state": "normal"}},
        }
        blob = _json.dumps(ws.build_openclaw_hook_payload(ev))
        self.assertNotIn("autoSendShadow", blob)
        self.assertNotIn("wouldAutoSend", blob)


if __name__ == "__main__":
    unittest.main()
