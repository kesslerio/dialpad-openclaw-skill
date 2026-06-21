"""Unit tests for the phone-first identity resolver (S2). Attio HTTP fully mocked.

Mirrors tests/test_attio_context.py: the Attio transport is mocked at
``attio_context._request`` so the suite never touches live Attio.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "adapters"))

import attio_context as attio  # noqa: E402
import identity_resolver as resolver  # noqa: E402


# --- confirmed people field shapes (verified live 2026-06-18) -----------------
PERSON_FULL = {
    "id": {"record_id": "person-1", "workspace_id": "ws-1", "object_id": "people"},
    "values": {
        "name": [{
            "first_name": "Jane",
            "last_name": "Doe",
            "full_name": "Jane Doe",
            "active_from": "2026-01-01T00:00:00Z",
            "active_until": None,
        }],
        "email_addresses": [{
            "email_address": "jane@acme.com",
            "original_email_address": "Jane@Acme.com",
            "email_domain": "acme.com",
            "active_from": "2026-01-01T00:00:00Z",
        }],
        "company": [{"target_record_id": "co-1", "target_object": "companies"}],
        "phone_numbers": [{"phone_number": "+14155201316"}],
    },
}

# A person Attio matches by phone but whose name fields are unusable.
PERSON_NO_NAME = {
    "id": {"record_id": "person-2"},
    "values": {
        "name": [None],
        "email_addresses": [],
        "phone_numbers": [{"phone_number": "+14155551234"}],
    },
}

# Matched by phone, no usable name, but DOES carry an email so the follow-up
# email stage has something to query.
PERSON_NO_NAME_WITH_EMAIL = {
    "id": {"record_id": "person-3"},
    "values": {
        "name": [None],
        "email_addresses": [{"email_address": "jane@acme.com", "active_until": None}],
        "phone_numbers": [{"phone_number": "+14155551234"}],
    },
}

COMPANY = {
    "id": {"record_id": "co-1"},
    "values": {"name": [{"value": "Acme Corp", "attribute_type": "text"}]},
}


def make_fake_request(person_for_phone=None, person_for_email=None, company=COMPANY):
    """Build a fake _request honoring the people-query filter and company GET."""
    def fake_request(method, path, body=None):
        if method == "POST" and path == "/objects/people/records/query":
            filt = (body or {}).get("filter") or {}
            if "phone_numbers" in filt and person_for_phone is not None:
                return {"data": [person_for_phone]}
            if "email_addresses" in filt and person_for_email is not None:
                return {"data": [person_for_email]}
            return {"data": []}
        if method == "GET" and path == "/objects/companies/records/co-1" and company is not None:
            return {"data": company}
        return {"data": []}
    return fake_request


def with_key(env=None):
    """Patch context so ATTIO_API_KEY is present (so _request reaches the HTTP layer)."""
    base = {"ATTIO_API_KEY": "test-key"}
    if env:
        base.update(env)
    return patch.dict("os.environ", base)


class AttioPhoneHighTests(unittest.TestCase):
    def test_attio_phone_high(self):
        fake = make_fake_request(person_for_phone=PERSON_FULL)
        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity("+1 (415) 520-1316")
        self.assertEqual(out["confidence"], "high")
        self.assertEqual(out["identity"]["name"], "Jane Doe")
        self.assertEqual(out["identity"]["first_name"], "Jane")
        self.assertEqual(out["identity"]["last_name"], "Doe")
        self.assertEqual(out["identity"]["email"], "jane@acme.com")
        self.assertEqual(out["identity"]["company"], "Acme Corp")
        self.assertIn("attio_phone", out["sources"])
        # Every identity field is a str or None (no type pollution from Attio).
        for value in out["identity"].values():
            self.assertTrue(value is None or isinstance(value, str))

    def test_attio_wins_over_dialpad_but_fills_gaps(self):
        # Attio name/email/company win; a Dialpad-only field with no Attio value stays.
        fake = make_fake_request(person_for_phone=PERSON_FULL)
        contact = {"name": "J. Doe (stale)", "company": None, "title": "VP"}
        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity("+14155201316", dialpad_contact=contact)
        self.assertEqual(out["confidence"], "high")
        self.assertEqual(out["identity"]["name"], "Jane Doe")  # Attio wins
        self.assertEqual(out["identity"]["company"], "Acme Corp")


class DialpadOnlyMediumTests(unittest.TestCase):
    def test_dialpad_only_medium(self):
        # Attio degraded/missing -> only the Dialpad name carries; medium.
        contact = {"first_name": "Bob", "last_name": "Smith", "company": "Globex"}
        with patch.dict("os.environ", {}, clear=True):  # no ATTIO_API_KEY
            out = resolver.resolve_identity("+14155550000", dialpad_contact=contact)
        self.assertEqual(out["confidence"], "medium")
        self.assertEqual(out["identity"]["name"], "Bob Smith")
        self.assertEqual(out["identity"]["company"], "Globex")
        self.assertIn("dialpad_contact", out["sources"])

    def test_attio_person_matched_no_name_is_medium(self):
        fake = make_fake_request(person_for_phone=PERSON_NO_NAME)
        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity("+14155551234")
        self.assertEqual(out["confidence"], "medium")
        self.assertIsNone(out["identity"]["name"])
        self.assertIn("attio_phone", out["sources"])


class EmailFollowupHighTests(unittest.TestCase):
    def test_email_followup_high(self):
        # Phone misses; a caller-supplied email resolves an Attio person -> high.
        fake = make_fake_request(person_for_phone=None, person_for_email=PERSON_FULL)
        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity("+14155559999", email="jane@acme.com")
        self.assertEqual(out["confidence"], "high")
        self.assertEqual(out["identity"]["name"], "Jane Doe")
        self.assertIn("attio_email", out["sources"])

    def test_email_from_dialpad_seeds_email_stage(self):
        # No caller email arg, but the Dialpad contact carries one.
        fake = make_fake_request(person_for_phone=None, person_for_email=PERSON_FULL)
        contact = {"email": "jane@acme.com"}
        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity("+14155559999", dialpad_contact=contact)
        self.assertEqual(out["confidence"], "high")
        self.assertIn("attio_email", out["sources"])

    def test_high_phone_match_short_circuits_email_stage(self):
        # Phone already resolved high; the email stage must NOT run.
        calls = []

        def fake(method, path, body=None):
            calls.append(((body or {}).get("filter") or {}))
            if method == "POST" and path == "/objects/people/records/query":
                filt = (body or {}).get("filter") or {}
                if "phone_numbers" in filt:
                    return {"data": [PERSON_FULL]}
            if method == "GET" and path == "/objects/companies/records/co-1":
                return {"data": COMPANY}
            return {"data": []}

        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity("+14155201316", email="someone@else.com")
        self.assertEqual(out["confidence"], "high")
        self.assertNotIn("attio_email", out["sources"])
        # No people query carried an email_addresses filter.
        self.assertFalse(any("email_addresses" in c for c in calls))


class NotFoundLowTests(unittest.TestCase):
    def test_not_found_low(self):
        fake = make_fake_request(person_for_phone=None, person_for_email=None)
        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity("+14155551111")
        self.assertEqual(out["confidence"], "low")
        self.assertIsNone(out["identity"]["name"])
        self.assertIsNone(out["identity"]["company"])

    def test_no_input_low(self):
        # No phone, no contact, no email -> low, network-free, no_input source.
        with patch.dict("os.environ", {}, clear=True):
            out = resolver.resolve_identity(None)
        self.assertEqual(out["confidence"], "low")
        self.assertEqual(out["sources"], ["no_input"])


class AttioDegradedLowTests(unittest.TestCase):
    def test_attio_degraded_low_records_error_and_falls_back(self):
        # Attio raises; no Dialpad name -> low, attio_error recorded, no raise.
        with with_key(), patch.object(attio, "_request", side_effect=attio.AttioError("http_500")):
            out = resolver.resolve_identity("+14155551234")
        self.assertEqual(out["confidence"], "low")
        self.assertIn("attio_error", out["sources"])

    def test_attio_error_does_not_elevate_medium_dialpad(self):
        # Dialpad gave a name (medium); Attio errors must not raise or change that.
        contact = {"name": "Carol King"}
        with with_key(), patch.object(attio, "_request", side_effect=attio.AttioError("network")):
            out = resolver.resolve_identity("+14155551234", dialpad_contact=contact)
        self.assertEqual(out["confidence"], "medium")
        self.assertEqual(out["identity"]["name"], "Carol King")
        self.assertIn("attio_error", out["sources"])

    def test_email_stage_attio_error_fails_closed(self):
        # Phone misses, email stage raises -> low, attio_error, no exception.
        def fake(method, path, body=None):
            filt = (body or {}).get("filter") or {}
            if "phone_numbers" in filt:
                return {"data": []}
            if "email_addresses" in filt:
                raise attio.AttioError("http_403")
            return {"data": []}

        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity("+14155559999", email="x@y.com")
        self.assertEqual(out["confidence"], "low")
        self.assertIn("attio_error", out["sources"])


class PersonFieldExtractionTests(unittest.TestCase):
    """Confirmed people shapes through attio_context helpers."""

    def test_person_name_parts(self):
        self.assertEqual(attio.person_name_parts(PERSON_FULL), ("Jane", "Doe", "Jane Doe"))

    def test_person_name_parts_builds_full_from_parts(self):
        person = {"values": {"name": [{"first_name": "Ann", "last_name": "Lee"}]}}
        self.assertEqual(attio.person_name_parts(person), ("Ann", "Lee", "Ann Lee"))

    def test_person_primary_email_lowercased(self):
        self.assertEqual(attio.person_primary_email(PERSON_FULL), "jane@acme.com")

    def test_person_company_name_prefers_direct_company(self):
        fake = make_fake_request(person_for_phone=PERSON_FULL)
        with with_key(), patch.object(attio, "_request", side_effect=fake):
            self.assertEqual(attio.person_company_name(PERSON_FULL), "Acme Corp")


class MalformedRecordTests(unittest.TestCase):
    def test_name_is_none_entry(self):
        self.assertEqual(attio.person_name_parts({"values": {"name": [None]}}), (None, None, None))

    def test_email_addresses_not_a_list(self):
        person = {"values": {"email_addresses": "nope@nope.com"}}
        self.assertIsNone(attio.person_primary_email(person))

    def test_name_not_a_list(self):
        person = {"values": {"name": {"first_name": "X"}}}
        self.assertEqual(attio.person_name_parts(person), (None, None, None))

    def test_non_dict_company_ref(self):
        person = {"values": {"company": ["not-a-dict"]}}
        self.assertIsNone(attio.person_company_name(person))

    def test_dialpad_contact_not_a_dict(self):
        with patch.dict("os.environ", {}, clear=True):
            out = resolver.resolve_identity("+14155550000", dialpad_contact="oops")
        self.assertEqual(out["confidence"], "low")

    def test_dialpad_non_string_name_parts_do_not_raise(self):
        # int first_name + no usable top-level name must not hit str.join.
        contact = {"first_name": 123, "last_name": None}
        with patch.dict("os.environ", {}, clear=True):
            out = resolver.resolve_identity("+14155550000", dialpad_contact=contact)
        self.assertEqual(out["confidence"], "low")
        self.assertIsNone(out["identity"]["name"])
        self.assertIsNone(out["identity"]["first_name"])

    def test_dialpad_non_string_fields_never_pollute_identity(self):
        # Non-string name/company must not leak into the contract as non-strings.
        contact = {"name": 5, "company": 99, "email": ["x@y.com"]}
        with patch.dict("os.environ", {}, clear=True):
            out = resolver.resolve_identity("+14155550000", dialpad_contact=contact)
        ident = out["identity"]
        for key, value in ident.items():
            self.assertTrue(value is None or isinstance(value, str), f"{key}={value!r}")

    def test_malformed_person_does_not_crash_resolver(self):
        bad_person = {"values": {"name": "wrong", "email_addresses": 5, "company": None}}
        fake = make_fake_request(person_for_phone=bad_person)
        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity("+14155551234")
        # Person matched but nothing usable -> medium, no crash.
        self.assertEqual(out["confidence"], "medium")
        self.assertIsNone(out["identity"]["name"])


class SecretLeakTests(unittest.TestCase):
    def test_secret_never_appears_in_output(self):
        with patch.dict("os.environ", {"ATTIO_API_KEY": "SENTINEL"}), \
             patch.object(attio, "_request", side_effect=attio.AttioError("http_403")):
            out = resolver.resolve_identity("+14155201316", dialpad_contact={"name": "Z"})
        self.assertNotIn("SENTINEL", json.dumps(out))


class ImportSafeTests(unittest.TestCase):
    def test_import_safe_no_network_when_key_unset(self):
        # With ATTIO_API_KEY unset, _request raises before any urlopen. Assert we
        # never even reach the HTTP layer (urlopen is not called).
        with patch.dict("os.environ", {}, clear=True), \
             patch("attio_context.urllib.request.urlopen") as urlopen:
            out = resolver.resolve_identity("+14155201316")
        urlopen.assert_not_called()
        self.assertEqual(out["confidence"], "low")

    def test_resolve_identity_returns_full_contract(self):
        with patch.dict("os.environ", {}, clear=True):
            out = resolver.resolve_identity(None)
        self.assertEqual(set(out), {"identity", "confidence", "sources"})
        self.assertEqual(
            set(out["identity"]),
            {"name", "first_name", "last_name", "email", "company"},
        )


class NoWebhookSideEffectTests(unittest.TestCase):
    def test_resolver_source_does_not_reference_webhook_server(self):
        # Auto-merge tier: the resolver must not import webhook_server (which
        # would drag its side effects in). Inspect the source, not global
        # sys.modules, which other tests in the suite may have populated.
        src = Path(resolver.__file__).read_text(encoding="utf-8")
        self.assertNotIn("webhook_server", src)
        self.assertNotIn("import webhook", src)


class CodexP2RegressionTests(unittest.TestCase):
    """Regression coverage for the five Codex P2 findings on PR #97."""

    # P2-1: a non-string phone must degrade, never raise (TypeError in re.sub).
    def test_numeric_phone_does_not_raise(self):
        # No API key -> no network; the entrypoint must still return a contract.
        with patch.dict("os.environ", {}, clear=True):
            out = resolver.resolve_identity(14155201316)  # int, not str
        self.assertEqual(set(out), {"identity", "confidence", "sources"})
        self.assertEqual(out["confidence"], "low")

    def test_numeric_phone_is_coerced_and_can_match(self):
        # A coerced numeric phone still reaches Attio and can resolve high.
        fake = make_fake_request(person_for_phone=PERSON_FULL)
        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity(14155201316)
        self.assertEqual(out["confidence"], "high")
        self.assertEqual(out["identity"]["name"], "Jane Doe")
        self.assertIn("attio_phone", out["sources"])

    def test_non_string_email_does_not_raise(self):
        with patch.dict("os.environ", {}, clear=True):
            out = resolver.resolve_identity("+14155550000", email=12345)
        self.assertEqual(out["confidence"], "low")
        self.assertIsNone(out["identity"]["email"])

    # P2-2: an Attio phone match with no usable name is medium even when Dialpad
    # already supplied a name, AND the email follow-up still runs.
    def test_attio_no_name_stays_medium_despite_dialpad_name(self):
        fake = make_fake_request(person_for_phone=PERSON_NO_NAME)
        contact = {"name": "Dialpad Jane"}
        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity("+14155551234", dialpad_contact=contact)
        # Dialpad name is retained, but confidence is NOT promoted to high.
        self.assertEqual(out["confidence"], "medium")
        self.assertEqual(out["identity"]["name"], "Dialpad Jane")
        self.assertIn("attio_phone", out["sources"])

    def test_attio_no_name_runs_email_followup_and_promotes(self):
        # Phone match has no name but yields an email; the email stage then finds a
        # named person and promotes to high. This only works if the no-name phone
        # match did NOT short-circuit on the pre-existing Dialpad name.
        fake = make_fake_request(
            person_for_phone=PERSON_NO_NAME_WITH_EMAIL,
            person_for_email=PERSON_FULL,
        )
        contact = {"name": "Dialpad Jane"}
        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity("+14155551234", dialpad_contact=contact)
        self.assertEqual(out["confidence"], "high")
        self.assertEqual(out["identity"]["name"], "Jane Doe")
        self.assertIn("attio_phone", out["sources"])
        self.assertIn("attio_email", out["sources"])

    # P2-3: a Dialpad contact's emails[] array seeds the email stage.
    def test_dialpad_emails_array_seeds_email_stage(self):
        fake = make_fake_request(person_for_phone=None, person_for_email=PERSON_FULL)
        contact = {"emails": ["jane@acme.com"]}
        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity("+14155559999", dialpad_contact=contact)
        self.assertEqual(out["confidence"], "high")
        self.assertEqual(out["identity"]["email"], "jane@acme.com")
        self.assertIn("attio_email", out["sources"])

    def test_dialpad_singular_email_preferred_over_emails_array(self):
        contact = {"email": "primary@acme.com", "emails": ["secondary@acme.com"]}
        with patch.dict("os.environ", {}, clear=True):
            out = resolver.resolve_identity("+14155550000", dialpad_contact=contact)
        self.assertEqual(out["identity"]["email"], "primary@acme.com")

    def test_dialpad_emails_array_non_string_entry_ignored(self):
        contact = {"emails": [12345]}
        with patch.dict("os.environ", {}, clear=True):
            out = resolver.resolve_identity("+14155550000", dialpad_contact=contact)
        self.assertIsNone(out["identity"]["email"])

    # P2-4: a real miss records its lookup source, not the misleading no_input.
    def test_phone_not_found_records_source_not_no_input(self):
        fake = make_fake_request(person_for_phone=None, person_for_email=None)
        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity("+14155551111")
        self.assertEqual(out["confidence"], "low")
        self.assertIn("attio_phone_not_found", out["sources"])
        self.assertNotIn("no_input", out["sources"])

    def test_email_not_found_records_source(self):
        # Phone misses; a caller email is supplied but Attio matches nothing.
        fake = make_fake_request(person_for_phone=None, person_for_email=None)
        with with_key(), patch.object(attio, "_request", side_effect=fake):
            out = resolver.resolve_identity("+14155551111", email="ghost@acme.com")
        self.assertIn("attio_phone_not_found", out["sources"])
        self.assertIn("attio_email_not_found", out["sources"])
        self.assertNotIn("no_input", out["sources"])

    def test_no_input_only_when_nothing_to_resolve_from(self):
        with patch.dict("os.environ", {}, clear=True):
            out = resolver.resolve_identity(None)
        self.assertEqual(out["sources"], ["no_input"])


if __name__ == "__main__":
    unittest.main()
