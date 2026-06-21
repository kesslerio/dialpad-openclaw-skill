"""Unit tests for the S5 Attio person-timeline note write-back. HTTP fully mocked.

No live network: the Attio HTTP layer (``attio_context._request``) is stubbed in
every test, so a captured-request assertion stands in for the live POST. The CP2
single-note live smoke is a deploy step, not a test here.

Safety properties under test:
  - write ONLY at high identity confidence (medium/low/None write nothing) — the
    cross-customer CRM-leak guard;
  - sensitive / opt-out messages are structurally ineligible -> no note;
  - a list sender_number normalizes via first_value (never a list to _normalize_phone);
  - no double-write on a repeated Dialpad message id;
  - all errors are swallowed (written=False, never raises, never blocks);
  - the flag defaults OFF and gates everything;
  - the POST path is the bare ``/notes`` (full URL ``.../v2/notes``, never v2/v2);
  - the body is the canonical data-wrapper with content_plaintext + parent_record_id.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "adapters"))

import attio_context  # noqa: E402
import webhook_server as ws  # noqa: E402

# Importing webhook_server caches the scripts/ copies of modules whose basenames
# also exist under bin/ (e.g. ``send_sms``). Other test files insert bin/ on
# sys.path and import the bin/ copy by the same name; a stale scripts/ entry in
# sys.modules would shadow theirs for the rest of the pytest session. webhook_server
# already holds its own references to these symbols, so evicting the bare names from
# sys.modules here leaves it working while letting later files import their bin/ copy.
for _dual_named in (
    "send_sms", "lookup_contact", "make_call", "export_sms", "list_calls",
    "create_sms_webhook", "create_sms_draft", "approve_sms_draft",
):
    sys.modules.pop(_dual_named, None)


PERSON = {
    "id": {"record_id": "REC123", "workspace_id": "ws-1", "object_id": "obj-1"},
    "values": {"name": [{"full_name": "Ada Lovelace", "active_until": None}]},
}
# An Attio match with no usable name -> S2 would rate this medium, not high.
PERSON_NO_NAME = {"id": {"record_id": "REC999"}, "values": {}}


def _event(
    *,
    confidence="high",
    text="Hi, I'd like a demo of ShapeScale please.",
    sender_number="+14155550123",
    message_id="msg-1",
    contact_name="Ada Lovelace",  # ties to PERSON's full_name (identity-match gate)
):
    return {
        "event_type": "sms",
        "text": text,
        "sender_number": sender_number,
        "recipient_number": "+14155201316",  # the Dialpad line — must NOT be used
        "message_id": message_id,
        "inbound_context": {"identityConfidence": confidence},
        "first_contact": {"contactName": contact_name, "knownContact": True},
    }


class CapturingRequest:
    """Stand-in for attio_context._request that records POSTs and returns a note id."""

    def __init__(self, raise_exc=None, note_id="NOTE-1", person=PERSON, people=None):
        self.calls = []
        self.raise_exc = raise_exc
        self.note_id = note_id
        self.person = person
        # ``people`` overrides the query result to test ambiguity (>1 match).
        self.people = people

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path == "/objects/people/records/query":
            if self.people is not None:
                return {"data": self.people}
            return {"data": [self.person] if self.person is not None else []}
        if method == "POST" and path == "/notes":
            if self.raise_exc is not None:
                raise self.raise_exc
            return {"data": {"id": {"note_id": self.note_id}}}
        return {"data": []}

    @property
    def note_posts(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == "/notes"]


class AttioNoteWritebackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = self.tmp.name
        # Enable the flag for the happy-path tests; individual tests override.
        self._flag = patch.object(ws, "DIALPAD_ATTIO_NOTE_WRITEBACK_ENABLED", True)
        self._flag.start()

    def tearDown(self):
        self._flag.stop()
        Path(self.db).unlink(missing_ok=True)

    def _write(self, event, fake):
        with patch.object(attio_context, "_request", fake), \
                patch.object(attio_context, "_api_key", lambda: "test-key"):
            return ws.write_attio_inbound_note(event, db_path=self.db)

    # 1 -------------------------------------------------------------------
    def test_high_confidence_writes_note_to_right_person(self):
        fake = CapturingRequest()
        result = self._write(_event(confidence="high"), fake)
        self.assertTrue(result["written"])
        self.assertEqual(result["status"], "written")
        self.assertEqual(result["note_id"], "NOTE-1")
        self.assertEqual(len(fake.note_posts), 1)
        _method, path, body = fake.note_posts[0]
        self.assertEqual(path, "/notes")
        data = body["data"]
        self.assertEqual(data["parent_object"], "people")
        self.assertEqual(data["parent_record_id"], "REC123")
        self.assertEqual(data["title"], "Inbound SMS")
        self.assertEqual(data["content_plaintext"], "Hi, I'd like a demo of ShapeScale please.")
        # content_plaintext is the live shape — NOT a format/content pair.
        self.assertNotIn("format", data)
        self.assertNotIn("content", data)

    # 2 -------------------------------------------------------------------
    def test_low_confidence_writes_nothing(self):
        fake = CapturingRequest()
        result = self._write(_event(confidence="low"), fake)
        self.assertFalse(result["written"])
        self.assertEqual(result["status"], "low_confidence")
        self.assertEqual(fake.calls, [])  # not even a person lookup

    # 3 -------------------------------------------------------------------
    def test_medium_confidence_writes_nothing(self):
        fake = CapturingRequest()
        result = self._write(_event(confidence="medium"), fake)
        self.assertFalse(result["written"])
        self.assertEqual(result["status"], "low_confidence")
        self.assertEqual(fake.note_posts, [])

    def test_high_context_but_no_attio_name_writes_nothing(self):
        # inbound-context high, but the phone match has no usable name -> S2 medium.
        fake = CapturingRequest(person=PERSON_NO_NAME)
        result = self._write(_event(confidence="high"), fake)
        self.assertFalse(result["written"])
        self.assertEqual(result["status"], "low_confidence")
        self.assertEqual(fake.note_posts, [])

    def test_none_confidence_writes_nothing(self):
        fake = CapturingRequest()
        event = _event()
        event["inbound_context"] = {}
        result = self._write(event, fake)
        self.assertFalse(result["written"])
        self.assertEqual(result["status"], "low_confidence")
        self.assertEqual(fake.calls, [])

    def test_ambiguous_phone_refuses_to_write(self):
        # Two people share the phone (shared / recycled / family-plan number).
        # Picking "first of many" could write to the WRONG person -> refuse.
        other = {
            "id": {"record_id": "REC456"},
            "values": {"name": [{"full_name": "Grace Hopper", "active_until": None}]},
        }
        fake = CapturingRequest(people=[PERSON, other])
        result = self._write(_event(confidence="high"), fake)
        self.assertFalse(result["written"])
        self.assertEqual(result["status"], "ambiguous_phone")
        self.assertEqual(fake.note_posts, [])

    # 4 + 5 (sensitive / opt-out are structurally ineligible) -------------
    def test_sensitive_message_is_ineligible_so_no_note(self):
        decision = ws.assess_inbound_sms_alert_eligibility(
            {"direction": "inbound", "from_number": "+14155550123", "text": "Your verification code is 123456"},
            from_number="+14155550123",
            text="Your verification code is 123456",
        )
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["reason_code"], "filtered_sensitive")
        # The call site is unreachable for ineligible messages; the note write only
        # runs inside `if inbound_alert_decision["eligible"]:`.

    def test_opt_out_message_is_ineligible_so_no_note(self):
        decision = ws.assess_inbound_sms_alert_eligibility(
            {"direction": "inbound", "from_number": "+14155550123", "text": "STOP"},
            from_number="+14155550123",
            text="STOP",
        )
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["reason_code"], "filtered_opt_out")

    # 6 -------------------------------------------------------------------
    def test_list_sender_number_normalized_and_writes(self):
        fake = CapturingRequest()
        event = _event(sender_number=["+1 (415) 555-0123"])
        result = self._write(event, fake)
        self.assertTrue(result["written"])
        self.assertEqual(len(fake.note_posts), 1)
        # The recipient (Dialpad line) must never be the parent record.
        self.assertEqual(fake.note_posts[0][2]["data"]["parent_record_id"], "REC123")
        # And the lookup used the normalized sender, not the list.
        query_calls = [c for c in fake.calls if c[1] == "/objects/people/records/query"]
        self.assertEqual(query_calls[0][2]["filter"]["phone_numbers"], "+14155550123")

    # 7 -------------------------------------------------------------------
    def test_no_double_write_on_retry(self):
        fake = CapturingRequest()
        first = self._write(_event(message_id="dup-1"), fake)
        second = self._write(_event(message_id="dup-1"), fake)
        self.assertTrue(first["written"])
        self.assertFalse(second["written"])
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(fake.note_posts), 1)  # exactly one POST across both

    def test_identity_mismatch_does_not_write(self):
        # Recycled/stale phone: Dialpad resolved "Bob Smith" but Attio's record for the
        # number is "Ada Lovelace" -> names don't tie -> fail closed, NO POST. This is
        # the exact cross-customer CRM leak the gate exists to prevent.
        fake = CapturingRequest()
        result = self._write(_event(contact_name="Bob Smith"), fake)
        self.assertFalse(result["written"])
        self.assertEqual(result["status"], "identity_mismatch")
        self.assertEqual(fake.note_posts, [])

    def test_partial_name_ties(self):
        # First-name-only Dialpad record still ties to the fuller Attio name.
        fake = CapturingRequest()
        result = self._write(_event(contact_name="Ada"), fake)
        self.assertTrue(result["written"])

    def test_long_message_truncated_with_ellipsis(self):
        # A multipart SMS (>160 chars) is clamped with a visible ellipsis marker so a
        # rep can see content was cut rather than silently losing the actionable part.
        fake = CapturingRequest()
        result = self._write(_event(text="A" * 400), fake)
        self.assertTrue(result["written"])
        content = fake.note_posts[0][2]["data"]["content_plaintext"]
        self.assertLessEqual(len(content), 160)
        self.assertTrue(content.endswith("\u2026"))

    def test_failed_post_keeps_claim_and_suppresses_retry(self):
        # The most consequential branch of claim-before-POST/never-release: first
        # delivery claims the message id then the POST raises -> error, and the claim
        # is intentionally NOT released. A retry with the SAME message_id must be a
        # duplicate with NO second POST (guards a double-write when the first POST may
        # have partially succeeded). A regression that released on error would pass the
        # other tests yet break this invariant.
        fail = CapturingRequest(raise_exc=attio_context.AttioError("boom"))
        first = self._write(_event(message_id="retry-1"), fail)
        self.assertFalse(first["written"])
        self.assertEqual(first["status"], "error")
        ok = CapturingRequest()  # would POST successfully if it were reached
        second = self._write(_event(message_id="retry-1"), ok)
        self.assertFalse(second["written"])
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(ok.note_posts, [])  # claim kept -> no second POST

    def test_missing_message_id_no_write(self):
        fake = CapturingRequest()
        result = self._write(_event(message_id=None), fake)
        self.assertFalse(result["written"])
        self.assertEqual(result["status"], "no_message_id")
        self.assertEqual(fake.note_posts, [])

    # 8 -------------------------------------------------------------------
    def test_exact_post_path_is_bare_notes(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            # OSError is caught by _request and wrapped as AttioError; we only need
            # the captured URL, not a real network round-trip.
            raise OSError("stop after url capture")

        with patch.object(attio_context, "_api_key", lambda: "test-key"), \
                patch("attio_context.urllib.request.urlopen", fake_urlopen):
            try:
                attio_context.create_person_note(PERSON, "hello")
            except attio_context.AttioError:
                pass  # network wrapper turns the RuntimeError into AttioError
        self.assertEqual(captured["url"], "https://api.attio.com/v2/notes")
        self.assertNotIn("/v2/v2/", captured["url"])

    # 9 -------------------------------------------------------------------
    def test_swallow_attio_error_never_raises(self):
        fake = CapturingRequest(raise_exc=attio_context.AttioError("network"))
        result = self._write(_event(), fake)
        self.assertFalse(result["written"])
        self.assertEqual(result["status"], "error")

    def test_swallow_generic_exception_never_raises(self):
        fake = CapturingRequest(raise_exc=ValueError("boom"))
        result = self._write(_event(), fake)
        self.assertFalse(result["written"])
        self.assertEqual(result["status"], "error")

    def test_error_does_not_block_subsequent_statements(self):
        fake = CapturingRequest(raise_exc=RuntimeError("kaboom"))
        ran_after = []
        result = self._write(_event(), fake)
        ran_after.append("reached")  # would not run if write_attio_inbound_note raised
        self.assertFalse(result["written"])
        self.assertEqual(ran_after, ["reached"])

    # 10 ------------------------------------------------------------------
    def test_missing_record_id_no_post(self):
        person = {"id": {}, "values": {"name": [{"full_name": "Ada Lovelace", "active_until": None}]}}
        fake = CapturingRequest(person=person)
        result = self._write(_event(), fake)
        self.assertFalse(result["written"])
        self.assertIn(result["status"], {"person_not_found", "no_record_id", "low_confidence"})
        self.assertEqual(fake.note_posts, [])

    def test_create_person_note_returns_none_without_record_id(self):
        with patch.object(attio_context, "_request") as req:
            out = attio_context.create_person_note({"id": {}}, "hi")
        self.assertIsNone(out)
        req.assert_not_called()

    def test_create_person_note_returns_none_on_blank_content(self):
        with patch.object(attio_context, "_request") as req:
            out = attio_context.create_person_note(PERSON, "   ")
        self.assertIsNone(out)
        req.assert_not_called()

    def test_create_person_note_clamps_to_160_chars(self):
        captured = {}

        def fake_request(method, path, body=None):
            captured["body"] = body
            return {"data": {"id": {"note_id": "NOTE-X"}}}

        long_text = "x" * 500
        with patch.object(attio_context, "_request", fake_request):
            out = attio_context.create_person_note(PERSON, long_text)
        self.assertEqual(out, "NOTE-X")
        self.assertEqual(len(captured["body"]["data"]["content_plaintext"]), 160)

    # 11 ------------------------------------------------------------------
    def test_disabled_flag_writes_nothing(self):
        fake = CapturingRequest()
        with patch.object(ws, "DIALPAD_ATTIO_NOTE_WRITEBACK_ENABLED", False):
            result = self._write(_event(), fake)
        self.assertFalse(result["written"])
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(fake.calls, [])  # no confidence/resolver work attempted


class AttioNoteDedupeFailClosedTests(unittest.TestCase):
    """The note dedupe fails CLOSED — the opposite of the intake claim's fail-open."""

    def test_dedupe_unavailable_skips_write(self):
        # An unwritable db path forces the claim to fail; the write must NOT proceed.
        fake = CapturingRequest()
        with patch.object(ws, "DIALPAD_ATTIO_NOTE_WRITEBACK_ENABLED", True), \
                patch.object(attio_context, "_request", fake), \
                patch.object(attio_context, "_api_key", lambda: "test-key"), \
                patch.object(ws, "_init_attio_note_dedupe_db", side_effect=OSError("disk full")):
            result = ws.write_attio_inbound_note(_event(), db_path="/nonexistent/x.db")
        self.assertFalse(result["written"])
        self.assertEqual(result["status"], "dedupe_unavailable")
        self.assertEqual(fake.note_posts, [])

    def test_claim_no_message_id_fails_closed(self):
        out = ws.claim_attio_note_writeback(None)
        self.assertFalse(out["claimed"])
        self.assertFalse(out["duplicate"])
        self.assertEqual(out["status"], "no_message_id")


class CallSiteWiringTests(unittest.TestCase):
    """Drives the real _process_inbound_post_ack to prove the call site is wired,
    fail-closed, and flag-gated — not just the helper in isolation."""

    def setUp(self):
        import io
        self.io = io
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = self.tmp.name

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def _build_handler(self, payload):
        import json as _json
        raw = _json.dumps(payload).encode("utf-8")
        handler = object.__new__(ws.DialpadWebhookHandler)
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = self.io.BytesIO(raw)
        handler.wfile = self.io.BytesIO()
        handler.client_address = ("127.0.0.1", 12345)
        status = {"code": None}
        handler.send_response = lambda code: status.__setitem__("code", code)
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        handler.send_error = lambda code, *_: status.__setitem__("code", code)
        return handler, status

    def _drive(self, *, flag, fake_request, eligible_decision=None, text="Hi, I'd like a demo please."):
        payload = {
            "direction": "inbound",
            "from_number": ["+14155550123"],  # list form — must normalize via first_value
            "to_number": "+14155201316",
            "text": text,
            "message_id": "wire-1",
        }
        eligible = eligible_decision or {
            "eligible": True, "reason_code": "ok", "sensitive_filtered": False,
            "notification_type": "sms",
        }
        patchers = [
            patch.object(ws, "DIALPAD_ATTIO_NOTE_WRITEBACK_ENABLED", flag),
            patch.object(ws, "verify_webhook_auth", lambda *a, **k: (True, "test")),
            patch.object(ws, "handle_sms_webhook", lambda data: {"stored": True, "message": {}}),
            patch.object(ws, "lookup_contact_enrichment",
                         lambda n: {"contact_name": "Ada Lovelace", "status": "resolved", "degraded": False}),
            patch.object(ws, "apply_payload_contact_fallback", lambda enr, data: enr),
            patch.object(ws, "assess_inbound_sms_alert_eligibility", lambda *a, **k: eligible),
            patch.object(ws, "build_inbound_context",
                         lambda *a, **k: {"identityConfidence": "high", "draftMode": "none"}),
            patch.object(ws, "should_send_proactive_reply", lambda *a, **k: False),
            patch.object(ws, "create_proactive_reply_draft",
                         lambda *a, **k: (False, None, None, None, None)),
            patch.object(ws, "log_auto_send_shadow", lambda *a, **k: None),
            patch.object(ws, "send_sms_to_openclaw_hooks", lambda *a, **k: (False, "ok")),
            patch.object(ws, "send_to_telegram", lambda *a, **k: None),
            patch.object(ws, "lookup_recent_sms_context", lambda *a, **k: None),
            # The call site invokes write_attio_inbound_note() with no db_path, so the
            # note dedupe falls back to _sms_dedupe_db_path() -> our temp db.
            patch.object(ws, "_sms_dedupe_db_path", lambda: Path(self.db)),
            patch.object(attio_context, "_request", fake_request),
            patch.object(attio_context, "_api_key", lambda: "test-key"),
        ]
        for p in patchers:
            p.start()
        try:
            handler, status = self._build_handler(payload)
            handler.handle_webhook()
            return status
        finally:
            for p in patchers:
                p.stop()

    def test_flag_on_high_confidence_posts_note_through_handler(self):
        fake = CapturingRequest()
        status = self._drive(flag=True, fake_request=fake)
        self.assertEqual(status["code"], 200)
        self.assertEqual(len(fake.note_posts), 1)
        body = fake.note_posts[0][2]["data"]
        self.assertEqual(body["parent_record_id"], "REC123")
        self.assertEqual(body["parent_object"], "people")
        self.assertIn("content_plaintext", body)

    def test_flag_off_posts_no_note_through_handler(self):
        fake = CapturingRequest()
        status = self._drive(flag=False, fake_request=fake)
        self.assertEqual(status["code"], 200)
        self.assertEqual(fake.note_posts, [])

    def test_sensitive_ineligible_posts_no_note_through_handler(self):
        # Flag ON, but the message is filtered_sensitive -> ineligible -> the note
        # write call site (inside `if eligible:`) is never reached.
        fake = CapturingRequest()
        decision = {"eligible": False, "reason_code": "filtered_sensitive",
                    "sensitive_filtered": True, "notification_type": "sms"}
        status = self._drive(flag=True, fake_request=fake, eligible_decision=decision,
                             text="Your verification code is 123456")
        self.assertEqual(status["code"], 200)
        self.assertEqual(fake.note_posts, [])

    def test_opt_out_ineligible_posts_no_note_through_handler(self):
        fake = CapturingRequest()
        decision = {"eligible": False, "reason_code": "filtered_opt_out",
                    "sensitive_filtered": False, "notification_type": "sms"}
        with patch.object(ws, "mark_opt_out_fail_closed", lambda *a, **k: True), \
                patch.object(ws, "invalidate_pending_sms_drafts", lambda **k: None):
            status = self._drive(flag=True, fake_request=fake, eligible_decision=decision,
                                 text="STOP")
        self.assertEqual(status["code"], 200)
        self.assertEqual(fake.note_posts, [])


if __name__ == "__main__":
    unittest.main()
