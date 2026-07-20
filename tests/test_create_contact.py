from pathlib import Path
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import create_contact
from _dialpad_compat import WrapperError


class CreateContactTests(unittest.TestCase):
    def _run_main(self, args):
        with patch.object(sys, "argv", ["bin/create_contact.py", *args]):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = create_contact.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def _get_option(self, cmd: list[str], flag: str):
        if flag not in cmd:
            return None
        return cmd[cmd.index(flag) + 1]

    def _get_json_option(self, cmd: list[str], flag: str):
        value = self._get_option(cmd, flag)
        if value is None:
            return None
        return json.loads(value)

    def test_create_contact_success_shared_create(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {"items": []}
            if cmd[:2] == ["contacts", "contacts.create"]:
                return {"id": "contact-123"}
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "Alice",
                "--last-name", "Miller",
                "--phone", "+14155550123",
                "--email", "alice@example.com",
                "--company-name", "Acme",
                "--job-title", "VP",
                "--extension", "101",
                "--url", "https://acme.example",
            ])

        self.assertEqual(code, 0)
        self.assertIn("Created shared contact:", out)
        self.assertEqual(err, "")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[1][:2], ["contacts", "contacts.create"])
        self.assertEqual(self._get_option(calls[1], "--first-name"), "Alice")
        self.assertEqual(self._get_option(calls[1], "--last-name"), "Miller")
        payload = self._get_json_option(calls[1], "--data")
        self.assertEqual(payload["phones"], ["+14155550123"])
        self.assertEqual(payload["emails"], ["alice@example.com"])
        self.assertEqual(payload["company_name"], "Acme")

    def test_build_create_contact_command_args_uses_required_flags_and_data_payload(self):
        payload = create_contact.build_payload(
            first_name="Phil",
            last_name="Stockton",
            phones=["+13174411610"],
            emails=["phil@example.com"],
            urls=["https://stockton.training/"],
            company_name="Stockton Training Grounds",
            job_title="Owner",
            extension="101",
            owner_id=None,
        )

        cmd = create_contact.build_create_contact_command_args(payload)

        self.assertEqual(cmd[:2], ["contacts", "contacts.create"])
        self.assertEqual(self._get_option(cmd, "--first-name"), "Phil")
        self.assertEqual(self._get_option(cmd, "--last-name"), "Stockton")
        payload_arg = self._get_json_option(cmd, "--data")
        self.assertEqual(payload_arg["company_name"], "Stockton Training Grounds")
        self.assertEqual(payload_arg["phones"], ["+13174411610"])
        self.assertEqual(payload_arg["emails"], ["phil@example.com"])
        self.assertEqual(payload_arg["urls"], ["https://stockton.training/"])

    def test_create_contact_api_error_propagates(self):
        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=WrapperError("permission denied")):
            code, out, err = self._run_main([
                "--first-name", "Bob",
                "--last-name", "Jones",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Error: permission denied", err)

    def test_create_contact_shared_scope_updates_existing(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {
                    "items": [
                        {
                            "id": "contact-555",
                            "first_name": "Existing",
                            "last_name": "User",
                            "phones": ["+14155550123"],
                        }
                    ]
                }
            if cmd[:2] == ["contacts", "contacts.update"]:
                return {"id": "contact-555"}
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "New",
                "--last-name", "Contact",
                "--phone", "+14155550123",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Updated shared contact:", out)
        self.assertEqual(calls[0][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[1][:2], ["contacts", "contacts.update"])
        payload = self._get_json_option(calls[1], "--data")
        self.assertEqual(payload["first_name"], "New")
        self.assertEqual(payload["last_name"], "Contact")
        # phone-matched update: the (identical) phone must not duplicate
        self.assertEqual(payload["phones"], ["+14155550123"])

    def test_email_matched_update_merges_new_phone_keeps_existing_primary(self):
        """Regression for the 2026-07-20 clobber: PATCH replaces `phones`
        wholesale, so an email-matched upsert carrying a different phone used
        to DROP every number already on the contact (a CRM-sourced upsert
        erased the calendar-sourced demo phone, and the lead's texts showed
        as a raw number). Synthetic identifiers — the incident shape, not the
        incident data."""
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {
                    "items": [
                        {
                            "id": "contact-77",
                            "first_name": "Dana",
                            "last_name": "Example",
                            "primary_phone": "+14155550100",
                            "phones": ["+14155550100"],
                            "primary_email": "dana@clinic.example",
                            "emails": ["dana@clinic.example"],
                            "urls": ["https://crm.example/person/1"],
                        }
                    ]
                }
            if cmd[:2] == ["contacts", "contacts.update"]:
                return {"id": "contact-77"}
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "Dana",
                "--last-name", "Example",
                "--phone", "+15035550111",
                "--email", "DANA@clinic.example",
                "--url", "https://example.com/deal",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(calls[1][:2], ["contacts", "contacts.update"])
        payload = self._get_json_option(calls[1], "--data")
        # existing primary stays first; the new phone is appended, never dropped
        self.assertEqual(payload["phones"], ["+14155550100", "+15035550111"])
        # emails dedupe case-insensitively, keeping the existing casing/primary
        self.assertEqual(payload["emails"], ["dana@clinic.example"])
        # urls union, existing first
        self.assertEqual(payload["urls"], ["https://crm.example/person/1", "https://example.com/deal"])

    def test_merged_update_payload_coerces_legacy_phones_to_e164(self):
        # contacts.update requires E.164; a legacy formatted entry replayed
        # verbatim would fail the whole PATCH. Coerce what can be coerced,
        # drop what the API would reject anyway.
        match = {
            "primary_phone": "(415) 555-0100",
            "phones": ["(415) 555-0100", "ext. 12"],
        }
        base = {"first_name": "A", "last_name": "B", "phones": ["+15035550111"]}
        merged = create_contact.merged_update_payload(match, base)
        self.assertEqual(merged["phones"], ["+14155550100", "+15035550111"])

    def test_merged_update_payload_ignores_non_string_api_entries(self):
        # Dialpad list fields can carry dict/None entries; str()-ing those
        # would replay fabricated identifiers ("{'number': ...}", "None")
        # into the CRM. Same contract as get_contact_list_values.
        match = {
            "primary_phone": "+14155550100",
            "phones": [{"number": "+14155550100", "extension": "12"}, None, 4155550100],
            "primary_email": "dana@clinic.example",
            "emails": [{"email": "dana@clinic.example"}, None],
        }
        base = {"first_name": "A", "last_name": "B", "phones": ["+15035550111"], "emails": []}
        merged = create_contact.merged_update_payload(match, base)
        self.assertEqual(merged["phones"], ["+14155550100", "+15035550111"])
        self.assertEqual(merged["emails"], ["dana@clinic.example"])

    def test_coerce_e164_drops_shapes_without_a_defensible_mapping(self):
        # A bare "+<digits>" guess passes the API's E.164 regex while being a
        # fabricated wrong number — drop instead of inventing.
        self.assertEqual(create_contact._coerce_e164("(415) 555-0100 x12"), "")  # 11 digits, not 1-leading
        self.assertEqual(create_contact._coerce_e164("555-0100"), "")  # 7-digit local
        self.assertEqual(create_contact._coerce_e164("1-415-555-0100"), "+14155550100")
        self.assertEqual(create_contact._coerce_e164("+442071838750"), "+442071838750")  # already E.164
        # explicit + with formatting: strip separators, keep the country code
        self.assertEqual(create_contact._coerce_e164("+44 20 7183 8750"), "+442071838750")
        # 10-digit NON-NANP local format must not be fabricated into +1...
        self.assertEqual(create_contact._coerce_e164("0412 345 678"), "")

    def test_local_update_payload_carries_no_owner_id(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {"items": [{"id": "local-5", "phones": ["+14155550100"]}]}
            if cmd[:2] == ["contacts", "contacts.update"]:
                return {"id": "local-5"}
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, _out, err = self._run_main([
                "--first-name", "A",
                "--last-name", "B",
                "--phone", "+14155550100",
                "--scope", "local",
                "--owner-id", "owner-9",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        update_calls = [c for c in calls if c[:2] == ["contacts", "contacts.update"]]
        self.assertEqual(len(update_calls), 1)
        payload = self._get_json_option(update_calls[0], "--data")
        self.assertNotIn("owner_id", payload)

    def test_merged_update_payload_dedupes_by_digit_normalization(self):
        # Same digit string in different formatting dedupes after E.164
        # coercion (key = exact normalized digits, matching the
        # is_duplicate_contact semantics).
        match = {
            "primary_phone": "+14155550123",
            "phones": ["+14155550123", "1-415-555-0123"],
        }
        base = {"first_name": "A", "last_name": "B", "phones": ["+14155550123"]}
        merged = create_contact.merged_update_payload(match, base)
        self.assertEqual(merged["phones"], ["+14155550123"])

    def test_create_contact_auto_scope_with_owner_targets_shared_and_local(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {"items": []}
            if cmd[:2] == ["contacts", "contacts.create"]:
                payload = self._get_json_option(cmd, "--data")
                if payload.get("owner_id") == "owner-9":
                    return {"id": "local-1"}
                return {"id": "shared-1"}
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "Sam",
                "--last-name", "Auto",
                "--phone", "+14155550123",
                "--owner-id", "owner-9",
                "--scope", "auto",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Created local contact for owner owner-9:", out)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[1][:2], ["contacts", "contacts.create"])
        self.assertEqual(calls[2][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[3][:2], ["contacts", "contacts.create"])

    def test_create_contact_local_scope_updates_existing_per_owner(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {
                    "items": [
                        {
                            "id": "contact-777",
                            "first_name": "Existing",
                            "last_name": "Local",
                            "phones": ["+14155550123"],
                        }
                    ]
                }
            if cmd[:2] == ["contacts", "contacts.update"]:
                return {"id": "contact-777"}
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "Local",
                "--last-name", "User",
                "--phone", "+14155550123",
                "--scope", "local",
                "--owner-id", "owner-11",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Updated local contact for owner owner-11:", out)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[1][:2], ["contacts", "contacts.update"])

    def test_create_contact_local_owner_not_found_warning(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {"items": []}
            if cmd[:2] == ["contacts", "contacts.create"]:
                payload = self._get_json_option(cmd, "--data")
                if payload.get("owner_id") == "missing-owner":
                    raise WrapperError("Request failed: 404 owner not found")
                return {"id": "shared-1"}
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "Warn",
                "--last-name", "Owner",
                "--phone", "+14155550123",
                "--scope", "both",
                "--owner-id", "missing-owner",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Warnings:", out)
        self.assertIn("Owner missing-owner not found", out)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[1][:2], ["contacts", "contacts.create"])
        self.assertEqual(calls[2][:2], ["contacts", "contacts.list"])
        self.assertEqual(calls[3][:2], ["contacts", "contacts.create"])

    def test_create_contact_rejects_ambiguous_shared_match(self):
        def fake_run_generated(cmd: list[str]):
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {
                    "items": [
                        {"id": "a1", "display_name": "Alice One", "phones": ["+14155550123"]},
                        {"id": "a2", "display_name": "Alice Two", "phones": ["+14155550123"]},
                    ]
                }
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "Alice",
                "--last-name", "User",
                "--phone", "+14155550123",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Ambiguous contact match", err)

    def test_create_contact_rejects_ambiguous_local_match(self):
        def fake_run_generated(cmd: list[str]):
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {
                    "items": [
                        {"id": "l1", "display_name": "Local One", "phones": ["+14155550123"]},
                        {"id": "l2", "display_name": "Local Two", "phones": ["+14155550123"]},
                    ]
                }
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--first-name", "Local",
                "--last-name", "User",
                "--phone", "+14155550123",
                "--scope", "local",
                "--owner-id", "owner-11",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Ambiguous contact match", err)

    def test_create_contact_rejects_zero_max_pages(self):
        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json"):
            code, out, err = self._run_main([
                "--first-name", "Invalid",
                "--last-name", "Pages",
                "--phone", "+14155550123",
                "--max-pages", "0",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Invalid --max-pages value. Use a positive integer.", err)

    def test_create_contact_rejects_negative_max_pages(self):
        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json"):
            code, out, err = self._run_main([
                "--first-name", "Invalid",
                "--last-name", "Pages",
                "--phone", "+14155550123",
                "--max-pages", "-1",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Invalid --max-pages value. Use a positive integer.", err)

    def test_create_contact_fails_when_generated_cli_unavailable(self):
        with patch(
            "create_contact.require_generated_cli",
            side_effect=WrapperError("Generated CLI not found at /tmp/generated/dialpad"),
        ):
            code, out, err = self._run_main([
                "--first-name", "Alice",
                "--last-name", "Miller",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Generated CLI not found", err)


if __name__ == "__main__":
    unittest.main()
