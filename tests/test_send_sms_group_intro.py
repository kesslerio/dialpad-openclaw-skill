from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import send_sms
from _dialpad_compat import WrapperError
from _dialpad_compat import run_legacy
import send_group_intro


class SendSmsWrapperTests(unittest.TestCase):
    def _run_main(self, module, args):
        with patch.object(sys, "argv", ["bin/send_sms.py", *args]):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = module.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_send_sms_requires_sender_without_flags_or_env(self):
        with patch("send_sms.generated_cli_available", return_value=True), \
                patch("send_sms.require_api_key"), \
                patch.dict("os.environ", {
                    "DIALPAD_DEFAULT_PROFILE": "",
                    "DIALPAD_DEFAULT_FROM_NUMBER": "",
                    "DIALPAD_PROFILE_WORK_FROM": "",
                    "DIALPAD_PROFILE_SALES_FROM": "",
                }, clear=False), \
                patch("send_sms.run_generated_json"):
            code, out, err = self._run_main(send_sms, [
                "--to", "+14155550111",
                "--message", "Hello",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("No sender resolved", err)
        self.assertIn("Provide --from", err)

    def test_send_sms_resolves_profile_mapping(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            return {"id": "msg-1", "status": "pending"}

        with patch("send_sms.generated_cli_available", return_value=True), \
                patch("send_sms.require_api_key"), \
                patch.dict("os.environ", {"DIALPAD_PROFILE_WORK_FROM": "+14153602954"}, clear=False), \
                patch("send_sms.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main(send_sms, [
                "--to", "+14155550111",
                "--profile", "work",
                "--message", "Hello",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Selected sender: +14153602954", out)
        payload = json.loads(calls[0][3])
        self.assertEqual(payload["from_number"], "+14153602954")

    def test_send_sms_profile_requires_configured_sender(self):
        with patch("send_sms.generated_cli_available", return_value=True), \
                patch("send_sms.require_api_key"), \
                patch.dict("os.environ", {"DIALPAD_PROFILE_WORK_FROM": ""}, clear=False), \
                patch("send_sms.run_generated_json"):
            code, out, err = self._run_main(send_sms, [
                "--to", "+14155550111",
                "--profile", "work",
                "--message", "Hello",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Profile 'work' is not configured", err)

    def test_send_sms_rejects_invalid_default_from_number(self):
        with patch("send_sms.generated_cli_available", return_value=True), \
                patch("send_sms.require_api_key"), \
                patch.dict("os.environ", {"DIALPAD_DEFAULT_FROM_NUMBER": "not-a-number"}, clear=False), \
                patch("send_sms.run_generated_json"):
            code, out, err = self._run_main(send_sms, [
                "--to", "+14155550111",
                "--message", "Hello",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Invalid sender number", err)

    def test_send_sms_conflict_between_from_and_profile(self):
        with patch("send_sms.generated_cli_available", return_value=True), \
                patch("send_sms.require_api_key"), \
                patch.dict("os.environ", {"DIALPAD_PROFILE_WORK_FROM": "+14153602954"}, clear=False), \
                patch("send_sms.run_generated_json"):
            code, out, err = self._run_main(send_sms, [
                "--to", "+14155550111",
                "--from", "+14155201316",
                "--profile", "work",
                "--message", "Hello",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--from conflicts with --profile", err)

    def test_send_sms_allows_profile_conflict_with_override(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            return {"id": "msg-1", "message_status": "pending"}

        with patch("send_sms.generated_cli_available", return_value=True), \
                patch("send_sms.require_api_key"), \
                patch.dict("os.environ", {"DIALPAD_PROFILE_WORK_FROM": "+14153602954"}, clear=False), \
                patch("send_sms.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main(send_sms, [
                "--to", "+14155550111",
                "--from", "+14155201316",
                "--profile", "work",
                "--allow-profile-mismatch",
                "--message", "Hello",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        payload = json.loads(calls[0][3])
        self.assertEqual(payload["from_number"], "+14155201316")
        self.assertIn("Selected sender: +14155201316", out)

    def test_send_sms_dry_run_does_not_call_api(self):
        with patch("send_sms.generated_cli_available", return_value=True), \
                patch("send_sms.require_api_key") as require_key, \
                patch("send_sms.run_generated_json") as run_json:
            code, out, err = self._run_main(send_sms, [
                "--to", "+14155550111",
                "--from", "+14155201316",
                "--message", "Hello",
                "--dry-run",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(require_key.call_count, 0)
        self.assertEqual(run_json.call_count, 0)
        self.assertIn("Dry run: SMS not sent", out)
        self.assertIn("Selected sender: +14155201316", out)

    def test_send_sms_rejects_new_flags_when_generated_cli_unavailable(self):
        with patch("send_sms.generated_cli_available", return_value=False), \
                patch("send_sms.run_legacy") as run_legacy:
            code, out, err = self._run_main(send_sms, [
                "--to", "+14155550111",
                "--message", "Hello",
                "--profile", "work",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(run_legacy.call_count, 0)
        self.assertIn("requires generated/dialpad", err)

    def test_run_legacy_resolves_scripts_directory_first(self):
        with patch("_dialpad_compat.subprocess.run", return_value=SimpleNamespace(returncode=0)) as mocked:
            code = run_legacy("send_sms.py", ["--help"])

        self.assertEqual(code, 0)
        invoked = mocked.call_args.args[0]
        self.assertEqual(Path(invoked[1]).parent.name, "scripts")
        self.assertEqual(Path(invoked[1]).name, "send_sms.py")


class SendGroupIntroTests(unittest.TestCase):
    def _run_main(self, args):
        with patch.object(sys, "argv", ["bin/send_group_intro.py", *args]):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = send_group_intro.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_send_group_intro_requires_confirm_share(self):
        with patch("send_group_intro.generated_cli_available", return_value=True), \
                patch("send_group_intro.require_api_key"):
            code, out, err = self._run_main([
                "--prospect", "+14155550111",
                "--reference", "+14155559999",
                "--from", "+14155201316",
                "--message", "Please connect",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("without --confirm-share", err)

    def test_send_group_intro_dry_run_outputs_structure(self):
        with patch("send_group_intro.generated_cli_available", return_value=True), \
                patch("send_group_intro.require_api_key") as require_key, \
                patch("send_group_intro.run_generated_json") as run_json:
            code, out, err = self._run_main([
                "--prospect", "+14155550111",
                "--reference", "+14155559999",
                "--from", "+14155201316",
                "--confirm-share",
                "--dry-run",
                "--json",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(require_key.call_count, 0)
        self.assertEqual(run_json.call_count, 0)
        parsed = json.loads(out)
        self.assertEqual(parsed["mode"], "mirrored_fallback")
        self.assertTrue(parsed["dry_run"])
        self.assertEqual(parsed["prospect"]["to"], "+14155550111")
        self.assertEqual(parsed["reference"]["to"], "+14155559999")

    def test_send_group_intro_success_sends_two_messages(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if len(calls) == 1:
                return {"id": "prospect-msg", "message_status": "pending"}
            return {"id": "reference-msg", "message_status": "pending"}

        with patch("send_group_intro.generated_cli_available", return_value=True), \
                patch("send_group_intro.require_api_key"), \
                patch("send_group_intro.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--prospect", "+14155550111",
                "--reference", "+14155559999",
                "--from", "+14155201316",
                "--confirm-share",
                "--json",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:2], ["sms", "send"])
        self.assertEqual(calls[1][:2], ["sms", "send"])
        payload_a = json.loads(calls[0][3])
        payload_b = json.loads(calls[1][3])
        self.assertEqual(payload_a["to_numbers"], ["+14155550111"])
        self.assertEqual(payload_b["to_numbers"], ["+14155559999"])
        self.assertEqual(payload_a["from_number"], "+14155201316")
        self.assertEqual(payload_b["from_number"], "+14155201316")
        parsed = json.loads(out)
        self.assertEqual(parsed["mode"], "mirrored_fallback")
        self.assertEqual(parsed["prospect"]["id"], "prospect-msg")
        self.assertEqual(parsed["reference"]["id"], "reference-msg")

    def test_send_group_intro_partial_failure_returns_first_message_id(self):
        calls: list[list[str]] = []

        def fake_run_generated(cmd: list[str]):
            calls.append(cmd)
            if len(calls) == 1:
                return {"id": "prospect-msg", "message_status": "pending"}
            raise WrapperError("Boom")

        with patch("send_group_intro.generated_cli_available", return_value=True), \
                patch("send_group_intro.require_api_key"), \
                patch("send_group_intro.run_generated_json", side_effect=fake_run_generated):
            code, out, err = self._run_main([
                "--prospect", "+14155550111",
                "--reference", "+14155559999",
                "--from", "+14155201316",
                "--confirm-share",
            ])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(len(calls), 2)
        self.assertIn("first_message_id=prospect-msg", err)
        self.assertIn("partial success", err.lower())


if __name__ == "__main__":
    unittest.main()
