from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import create_contact
import create_sms_webhook
import export_sms
import list_sms_thread
import lookup_contact
import make_call
import send_sms
import sync_sms_export
import update_contact
from _dialpad_compat import ERROR_CODES, WrapperError

LIST_CALLS_SPEC = importlib.util.spec_from_file_location(
    "bin_list_calls_contract",
    Path(__file__).resolve().parent.parent / "bin" / "list_calls.py",
)
assert LIST_CALLS_SPEC is not None and LIST_CALLS_SPEC.loader is not None
list_calls_wrapper = importlib.util.module_from_spec(LIST_CALLS_SPEC)
LIST_CALLS_SPEC.loader.exec_module(list_calls_wrapper)

GET_CALL_TRANSCRIPT_SPEC = importlib.util.spec_from_file_location(
    "bin_get_call_transcript_contract",
    Path(__file__).resolve().parent.parent / "bin" / "get_call_transcript.py",
)
assert GET_CALL_TRANSCRIPT_SPEC is not None and GET_CALL_TRANSCRIPT_SPEC.loader is not None
get_call_transcript_wrapper = importlib.util.module_from_spec(GET_CALL_TRANSCRIPT_SPEC)
GET_CALL_TRANSCRIPT_SPEC.loader.exec_module(get_call_transcript_wrapper)


class JsonContractTests(unittest.TestCase):
    def _run(self, module, argv: list[str]) -> tuple[int, str, str]:
        with patch.object(sys, "argv", argv):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = module.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def _parse(self, raw: str) -> dict[str, object]:
        parsed = json.loads(raw)
        self.assertIsInstance(parsed, dict)
        return parsed

    def _assert_success(self, parsed: dict[str, object], command: str) -> None:
        self.assertEqual(set(parsed.keys()), {"ok", "command", "data", "meta"})
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["command"], command)
        self.assertEqual(parsed["meta"]["schema_version"], "1")
        self.assertTrue(parsed["meta"]["wrapper"])
        self.assertTrue(parsed["meta"]["timestamp_utc"])

    def _assert_error(self, parsed: dict[str, object], command: str) -> None:
        self.assertEqual(set(parsed.keys()), {"ok", "command", "error", "meta"})
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["command"], command)
        self.assertIn(parsed["error"]["code"], ERROR_CODES)
        self.assertIsInstance(parsed["error"]["retryable"], bool)
        self.assertEqual(parsed["meta"]["schema_version"], "1")

    def test_send_sms_json_success_envelope(self):
        with patch("send_sms.require_generated_cli"), \
                patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                patch("send_sms.run_generated_json", return_value={"id": "msg-1", "message_status": "pending"}), \
                patch("send_sms.require_api_key"):
            code, out, err = self._run(
                send_sms,
                ["bin/send_sms.py", "--to", "+14155550111", "--message", "hello", "--from", "+14155201316", "--json"],
            )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_success(parsed, "send_sms.send")
        self.assertEqual(parsed["data"]["status"], "accepted/queued")
        self.assertEqual(parsed["data"]["status_raw"], "pending")
        self.assertEqual(parsed["data"]["message_status"], "pending")
        self.assertEqual(parsed["data"]["delivery_status"], "accepted/queued")
        self.assertEqual(parsed["data"]["delivery_status_raw"], "pending")

    def test_send_sms_json_success_envelope_status_fallback(self):
        with patch("send_sms.require_generated_cli"), \
                patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                patch("send_sms.run_generated_json", return_value={"id": "msg-2", "status": "pending"}), \
                patch("send_sms.require_api_key"):
            code, out, err = self._run(
                send_sms,
                ["bin/send_sms.py", "--to", "+14155550111", "--message", "hello", "--from", "+14155201316", "--json"],
            )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_success(parsed, "send_sms.send")
        self.assertEqual(parsed["data"]["status"], "accepted/queued")
        self.assertEqual(parsed["data"]["status_raw"], "pending")
        self.assertEqual(parsed["data"]["delivery_status"], "accepted/queued")
        self.assertEqual(parsed["data"]["delivery_status_raw"], "pending")
        self.assertNotIn("message_status", parsed["data"])

    def test_send_sms_json_error_envelope(self):
        with patch(
            "send_sms.require_generated_cli",
            side_effect=WrapperError("Generated CLI not found", code="missing_generated_cli", retryable=False),
        ):
            code, out, err = self._run(send_sms, ["bin/send_sms.py", "--to", "+14155550111", "--message", "hello", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        self._assert_error(self._parse(out), "send_sms.send")

    def test_send_sms_argparse_failure_is_json_envelope(self):
        code, out, err = self._run(send_sms, ["bin/send_sms.py", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_error(parsed, "send_sms.send")
        self.assertEqual(parsed["error"]["code"], "invalid_argument")

    def test_send_sms_blocks_suspicious_stripped_currency(self):
        with patch("send_sms.require_generated_cli"), \
                patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                patch("send_sms.run_generated_json") as run_generated_json, \
                patch("send_sms.require_api_key"):
            code, out, err = self._run(
                send_sms,
                [
                    "bin/send_sms.py",
                    "--to",
                    "+14155550111",
                    "--message",
                    "Your lease buyout: ,035 (10% off + ,956 credit). Financing: ~45-156/month. That's about 0 LESS than your current 99/month lease.",
                    "--from",
                    "+14155201316",
                    "--json",
                ],
            )

        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        run_generated_json.assert_not_called()
        parsed = self._parse(out)
        self._assert_error(parsed, "send_sms.send")
        self.assertEqual(parsed["error"]["code"], "invalid_argument")
        self.assertIn("stripped currency", parsed["error"]["message"])

    def test_send_sms_blocks_suspicious_multidigit_stripped_currency(self):
        with patch("send_sms.require_generated_cli"), \
                patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                patch("send_sms.run_generated_json") as run_generated_json, \
                patch("send_sms.require_api_key"):
            code, out, err = self._run(
                send_sms,
                [
                    "bin/send_sms.py",
                    "--to",
                    "+14155550111",
                    "--message",
                    "Your lease buyout: 0,035 after discount.",
                    "--from",
                    "+14155201316",
                    "--json",
                ],
            )

        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        run_generated_json.assert_not_called()
        parsed = self._parse(out)
        self._assert_error(parsed, "send_sms.send")
        self.assertEqual(parsed["error"]["code"], "invalid_argument")

    def test_send_sms_blocks_suspicious_large_stripped_currency(self):
        with patch("send_sms.require_generated_cli"), \
                patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                patch("send_sms.run_generated_json") as run_generated_json, \
                patch("send_sms.require_api_key"):
            code, out, err = self._run(
                send_sms,
                [
                    "bin/send_sms.py",
                    "--to",
                    "+14155550111",
                    "--message",
                    "Your lease buyout: 20,035 after discount.",
                    "--from",
                    "+14155201316",
                    "--json",
                ],
            )

        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        run_generated_json.assert_not_called()
        parsed = self._parse(out)
        self._assert_error(parsed, "send_sms.send")
        self.assertEqual(parsed["error"]["code"], "invalid_argument")

    def test_send_sms_allows_valid_currency(self):
        with patch("send_sms.require_generated_cli"), \
                patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                patch("send_sms.run_generated_json", return_value={"id": "msg-3", "message_status": "pending"}) as run_generated_json, \
                patch("send_sms.require_api_key"):
            code, out, err = self._run(
                send_sms,
                [
                    "bin/send_sms.py",
                    "--to",
                    "+14155550111",
                    "--message",
                    "Your lease buyout: $7,035. Financing: ~$145-156/month. That's about $50 LESS than your current $199/month lease.",
                    "--from",
                    "+14155201316",
                    "--json",
                ],
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        run_generated_json.assert_called_once()
        self._assert_success(self._parse(out), "send_sms.send")

    def test_send_sms_allows_usd_prefixed_currency(self):
        with patch("send_sms.require_generated_cli"), \
                patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                patch("send_sms.run_generated_json", return_value={"id": "msg-usd", "message_status": "pending"}) as run_generated_json, \
                patch("send_sms.require_api_key"):
            code, out, err = self._run(
                send_sms,
                [
                    "bin/send_sms.py",
                    "--to",
                    "+14155550111",
                    "--message",
                    "Quote is USD 20,035 after discount.",
                    "--from",
                    "+14155201316",
                    "--json",
                ],
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        run_generated_json.assert_called_once()
        self._assert_success(self._parse(out), "send_sms.send")

    def test_send_sms_allows_non_currency_thousands(self):
        with patch("send_sms.require_generated_cli"), \
                patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                patch("send_sms.run_generated_json", return_value={"id": "msg-5", "message_status": "pending"}) as run_generated_json, \
                patch("send_sms.require_api_key"):
            code, out, err = self._run(
                send_sms,
                [
                    "bin/send_sms.py",
                    "--to",
                    "+14155550111",
                    "--message",
                    "We had 1,000 attendees and 2,500 check-ins this month.",
                    "--from",
                    "+14155201316",
                    "--json",
                ],
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        run_generated_json.assert_called_once()
        self._assert_success(self._parse(out), "send_sms.send")

    def test_send_sms_allows_non_currency_lease_and_monthly_thousands(self):
        with patch("send_sms.require_generated_cli"), \
                patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                patch("send_sms.run_generated_json", return_value={"id": "msg-6", "message_status": "pending"}) as run_generated_json, \
                patch("send_sms.require_api_key"):
            code, out, err = self._run(
                send_sms,
                [
                    "bin/send_sms.py",
                    "--to",
                    "+14155550111",
                    "--message",
                    "Your lease includes 10,000 annual miles and monthly allowance is 1,000 minutes.",
                    "--from",
                    "+14155201316",
                    "--json",
                ],
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        run_generated_json.assert_called_once()
        self._assert_success(self._parse(out), "send_sms.send")

    def test_send_sms_allows_explicit_suspicious_currency_override(self):
        with patch("send_sms.require_generated_cli"), \
                patch("send_sms.resolve_sender", return_value=("+14155201316", "--from")), \
                patch("send_sms.run_generated_json", return_value={"id": "msg-4", "message_status": "pending"}) as run_generated_json, \
                patch("send_sms.require_api_key"):
            code, out, err = self._run(
                send_sms,
                [
                    "bin/send_sms.py",
                    "--to",
                    "+14155550111",
                    "--message",
                    "Financing: ~45-156/month.",
                    "--allow-suspicious-currency",
                    "--from",
                    "+14155201316",
                    "--json",
                ],
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        run_generated_json.assert_called_once()
        self._assert_success(self._parse(out), "send_sms.send")

    def test_lookup_contact_invalid_argument_in_json_mode(self):
        code, out, err = self._run(lookup_contact, ["bin/lookup_contact.py", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_error(parsed, "lookup_contact.lookup")
        self.assertEqual(parsed["error"]["code"], "invalid_argument")

    def test_make_call_json_success_envelope(self):
        with patch("make_call.require_generated_cli"), \
                patch("make_call.require_api_key"), \
                patch("make_call.resolve_user_id", return_value="u1"), \
                patch("make_call.run_generated_json", return_value={"call_id": "c1"}):
            code, out, err = self._run(
                make_call,
                ["bin/make_call.py", "--to", "+14155550111", "--user-id", "u1", "--json"],
            )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self._assert_success(self._parse(out), "make_call.call")

    def test_list_calls_json_success_envelope(self):
        with patch.object(list_calls_wrapper, "require_api_key"), \
                patch.object(
                    list_calls_wrapper,
                    "fetch_calls",
                    return_value=[
                        {
                            "call_id": "call-1",
                            "date_started": 1742900400000,
                            "duration": 9000,
                            "direction": "outbound",
                            "contact": {"name": "Prospect"},
                        }
                    ],
                ):
            code, out, err = self._run(
                list_calls_wrapper,
                ["bin/list_calls.py", "--limit", "1", "--json"],
            )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self._assert_success(self._parse(out), "list_calls.list")

    def test_create_contact_json_success_envelope(self):
        def fake_run(cmd: list[str]):
            if cmd[:2] == ["contacts", "contacts.list"]:
                return {"items": []}
            if cmd[:2] == ["contacts", "contacts.create"]:
                return {"id": "ct-1"}
            raise AssertionError(cmd)

        with patch("create_contact.require_generated_cli"), \
                patch("create_contact.require_api_key"), \
                patch("create_contact.run_generated_json", side_effect=fake_run):
            code, out, err = self._run(
                create_contact,
                ["bin/create_contact.py", "--first-name", "A", "--last-name", "B", "--phone", "+14155550111", "--json"],
            )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self._assert_success(self._parse(out), "create_contact.upsert")

    def test_update_contact_not_found_maps_to_not_found(self):
        with patch("update_contact.require_generated_cli"), \
                patch("update_contact.require_api_key"), \
                patch("update_contact.run_generated_json", side_effect=WrapperError("404 Not Found")):
            code, out, err = self._run(
                update_contact,
                ["bin/update_contact.py", "--id", "missing", "--first-name", "A", "--json"],
            )
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_error(parsed, "update_contact.update")
        self.assertEqual(parsed["error"]["code"], "not_found")

    def test_create_sms_webhook_subcommand_list_json(self):
        with patch("create_sms_webhook.require_generated_cli"), \
                patch("create_sms_webhook.require_api_key"), \
                patch("create_sms_webhook.run_generated_json", return_value={"items": [{"id": "s1"}]}):
            code, out, err = self._run(create_sms_webhook, ["bin/create_sms_webhook.py", "list", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self._assert_success(self._parse(out), "create_sms_webhook.list")

    def test_create_sms_webhook_missing_subcommand_is_json_envelope(self):
        code, out, err = self._run(create_sms_webhook, ["bin/create_sms_webhook.py", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_error(parsed, "create_sms_webhook.create")
        self.assertEqual(parsed["error"]["code"], "invalid_argument")

    def test_create_sms_webhook_subcommand_delete_json(self):
        with patch("create_sms_webhook.require_generated_cli"), \
                patch("create_sms_webhook.require_api_key"), \
                patch("create_sms_webhook.run_generated_json", return_value={"ok": True}):
            code, out, err = self._run(create_sms_webhook, ["bin/create_sms_webhook.py", "delete", "sub-1", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self._assert_success(self._parse(out), "create_sms_webhook.delete")

    def test_create_sms_webhook_webhooks_list_json(self):
        with patch("create_sms_webhook.require_generated_cli"), \
                patch("create_sms_webhook.require_api_key"), \
                patch("create_sms_webhook.run_generated_json", return_value={"items": [{"id": "w1"}]}):
            code, out, err = self._run(create_sms_webhook, ["bin/create_sms_webhook.py", "webhooks", "list", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self._assert_success(self._parse(out), "create_sms_webhook.webhooks_list")

    def test_export_sms_json_success_has_single_json_output(self):
        call_count = {"n": 0}

        def fake_run(cmd: list[str]):
            if cmd[:2] == ["stats", "stats.create"]:
                self.assertIn("--export-type", cmd)
                self.assertIn("records", cmd)
                self.assertIn("--stat-type", cmd)
                self.assertIn("texts", cmd)
                return {"request_id": "r1"}
            if cmd[:2] == ["stats", "stats.get"]:
                call_count["n"] += 1
                return {"status": "complete", "download_url": "http://example.com/file.csv"}
            raise AssertionError(cmd)

        with patch("export_sms.require_generated_cli"), \
                patch("export_sms.require_api_key"), \
                patch("export_sms.run_generated_json", side_effect=fake_run):
            code, out, err = self._run(export_sms, ["bin/export_sms.py", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_success(parsed, "export_sms.export")
        self.assertIn("progress", parsed["meta"])

    def test_export_sms_date_filters_become_stats_cli_options(self):
        commands = []

        def fake_run(cmd: list[str]):
            commands.append(cmd)
            if cmd[:2] == ["stats", "stats.create"]:
                return {"id": "r1"}
            if cmd[:2] == ["stats", "stats.get"]:
                return {"status": "complete"}
            raise AssertionError(cmd)

        with patch("export_sms.require_generated_cli"), \
                patch("export_sms.require_api_key"), \
                patch("export_sms.run_generated_json", side_effect=fake_run), \
                patch("export_sms.date") as fake_date:
            fake_date.today.return_value = date(2026, 5, 14)
            code, out, err = self._run(
                export_sms,
                ["bin/export_sms.py", "--start-date", "2026-05-13", "--end-date", "2026-05-14", "--json"],
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self._assert_success(self._parse(out), "export_sms.export")
        self.assertEqual(commands[0], [
            "stats",
            "stats.create",
            "--export-type",
            "records",
            "--stat-type",
            "texts",
            "--days-ago-start",
            "1",
            "--days-ago-end",
            "0",
        ])

    def test_export_sms_timeout_maps_to_timeout(self):
        with patch("export_sms.require_generated_cli"), \
                patch("export_sms.require_api_key"), \
                patch("export_sms.run_generated_json", return_value={"request_id": "r1"}), \
                patch("export_sms.poll_for_completion", side_effect=WrapperError("Timed out after 10 seconds")):
            code, out, err = self._run(export_sms, ["bin/export_sms.py", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_error(parsed, "export_sms.export")
        self.assertEqual(parsed["error"]["code"], "timeout")

    def test_list_calls_argparse_failure_is_json_envelope(self):
        code, out, err = self._run(list_calls_wrapper, ["bin/list_calls.py", "--limit", "0", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_error(parsed, "list_calls.list")
        self.assertEqual(parsed["error"]["code"], "invalid_argument")

    def test_get_call_transcript_json_success_envelope(self):
        with patch.object(get_call_transcript_wrapper, "require_api_key"), \
                patch.object(
                    get_call_transcript_wrapper,
                    "resolve_call_transcript",
                    return_value={
                        "call_id": "call-123",
                        "available": True,
                        "transcript_text": "Transcript body",
                        "transcript_review_url": None,
                        "source": "transcripts",
                        "unavailable_reason": None,
                    },
                ):
            code, out, err = self._run(
                get_call_transcript_wrapper,
                ["bin/get_call_transcript.py", "--call-id", "call-123", "--json"],
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_success(parsed, "get_call_transcript.get")
        self.assertEqual(parsed["data"]["call_id"], "call-123")
        self.assertTrue(parsed["data"]["available"])

    def test_get_call_transcript_argparse_failure_is_json_envelope(self):
        code, out, err = self._run(
            get_call_transcript_wrapper,
            ["bin/get_call_transcript.py", "--call-id", "call-123", "--with", "Jane", "--json"],
        )
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_error(parsed, "get_call_transcript.get")
        self.assertEqual(parsed["error"]["code"], "invalid_argument")

    def test_list_sms_thread_json_success_reports_outbound_state(self):
        class FakeConn:
            def close(self):
                pass

        messages = [
            {
                "dialpad_id": 1,
                "direction": "inbound",
                "from_number": "+14155550123",
                "to_number": "+14155201316",
                "contact_name": "Jane Doe",
                "timestamp": 1770000000000,
                "text": "Question",
            },
            {
                "dialpad_id": 2,
                "direction": "outbound",
                "from_number": "+14155201316",
                "to_number": "+14155550123",
                "contact_name": "Jane Doe",
                "timestamp": 1770000060000,
                "message_status": "sent",
                "delivery_result": "success",
                "text": "Answer",
            },
        ]

        with patch("list_sms_thread.init_db", return_value=FakeConn()), \
                patch(
                    "list_sms_thread.load_thread_summary",
                    return_value={
                        "phone": "+14155550123",
                        "count": 2,
                        "outbound_count": 1,
                        "inbound_count": 1,
                        "has_outbound": True,
                        "latest_outbound_timestamp": 1770000060000,
                        "latest_outbound_timestamp_utc": "2026-02-02T08:41:00Z",
                        "messages": [list_sms_thread._summarize_message(message) for message in messages],
                    },
                ):
            code, out, err = self._run(
                list_sms_thread,
                ["bin/list_sms_thread.py", "--phone", "+14155550123", "--json"],
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_success(parsed, "list_sms_thread.list")
        self.assertEqual(parsed["data"]["phone"], "+14155550123")
        self.assertEqual(parsed["data"]["count"], 2)
        self.assertEqual(parsed["data"]["outbound_count"], 1)
        self.assertTrue(parsed["data"]["has_outbound"])

    def test_list_sms_thread_empty_thread_is_success(self):
        class FakeConn:
            def close(self):
                pass

        with patch("list_sms_thread.init_db", return_value=FakeConn()), \
                patch(
                    "list_sms_thread.load_thread_summary",
                    return_value={
                        "phone": "+14155550123",
                        "count": 0,
                        "outbound_count": 0,
                        "inbound_count": 0,
                        "has_outbound": False,
                        "latest_outbound_timestamp": None,
                        "latest_outbound_timestamp_utc": None,
                        "messages": [],
                    },
                ):
            code, out, err = self._run(
                list_sms_thread,
                ["bin/list_sms_thread.py", "--phone", "+14155550123", "--json"],
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_success(parsed, "list_sms_thread.list")
        self.assertEqual(parsed["data"]["count"], 0)
        self.assertFalse(parsed["data"]["has_outbound"])

    def test_list_sms_thread_argparse_failure_is_json_envelope(self):
        code, out, err = self._run(
            list_sms_thread,
            ["bin/list_sms_thread.py", "--phone", "+14155550123", "--limit", "0", "--json"],
        )
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_error(parsed, "list_sms_thread.list")
        self.assertEqual(parsed["error"]["code"], "invalid_argument")

    def test_list_sms_thread_counts_full_thread_not_only_returned_slice(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                dialpad_id INTEGER,
                contact_number TEXT,
                contact_name TEXT,
                direction TEXT,
                from_number TEXT,
                to_number TEXT,
                text TEXT,
                message_status TEXT,
                delivery_result TEXT,
                timestamp INTEGER
            )
            """
        )
        for idx in range(25):
            conn.execute(
                """
                INSERT INTO messages (
                    dialpad_id, contact_number, contact_name, direction,
                    from_number, to_number, text, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idx + 1,
                    "+14155550123",
                    "Jane Doe",
                    "outbound" if idx == 0 else "inbound",
                    "+14155201316" if idx == 0 else "+14155550123",
                    "+14155550123" if idx == 0 else "+14155201316",
                    f"message {idx}",
                    1770000000000 + idx,
                ),
            )
        conn.commit()

        summary = list_sms_thread.load_thread_summary(conn, "+14155550123", limit=5)

        self.assertEqual(summary["count"], 25)
        self.assertEqual(summary["outbound_count"], 1)
        self.assertTrue(summary["has_outbound"])
        self.assertEqual(len(summary["messages"]), 5)

    def test_list_sms_thread_filters_messages_before_summary(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                dialpad_id INTEGER,
                contact_number TEXT,
                contact_name TEXT,
                direction TEXT,
                from_number TEXT,
                to_number TEXT,
                text TEXT,
                message_status TEXT,
                delivery_result TEXT,
                timestamp INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO messages (
                dialpad_id, contact_number, contact_name, direction,
                from_number, to_number, text, timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "+14155550123",
                "Jane Doe",
                "inbound",
                "+14155550123",
                "+14155201316",
                "raw sensitive text",
                1770000000000,
            ),
        )
        conn.commit()

        def redact(messages):
            return [{**message, "text": "[redacted]"} for message in messages]

        with patch("list_sms_thread.filter_messages", side_effect=redact) as filter_messages:
            summary = list_sms_thread.load_thread_summary(conn, "+14155550123", limit=5)

        filter_messages.assert_called_once()
        self.assertEqual(summary["messages"][0]["text"], "[redacted]")

    def test_sync_sms_export_json_dry_run_imports_export_rows(self):
        class FakeConn:
            def close(self):
                pass

        csv_path = Path("/tmp/test-sync-sms-export.csv")
        csv_path.write_text(
            '"date","message_id","name","email","target_type","target_id","sender_id","direction","to_phone","from_phone","encrypted_text","encrypted_aes_text","mms","timezone"\n'
            '"2026-05-08 03:17:13.418699","6676061264355328","Sales","","department","6500922273529856","6500922273529856","internal","+16694009313","+14155201316","","","","UTC"\n',
            encoding="utf-8",
        )

        with patch("sync_sms_export.init_db", return_value=FakeConn()), \
                patch("sync_sms_export.message_exists", return_value=False), \
                patch("sync_sms_export.store_message") as store_message:
            code, out, err = self._run(
                sync_sms_export,
                ["bin/sync_sms_export.py", "--input-csv", str(csv_path), "--dry-run", "--json"],
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_success(parsed, "sync_sms_export.sync")
        self.assertEqual(parsed["data"]["rows"], 1)
        self.assertEqual(parsed["data"]["imported"], 1)
        store_message.assert_not_called()

    def test_sync_sms_export_skips_existing_rows_to_preserve_webhook_text(self):
        class FakeConn:
            def close(self):
                pass

        csv_path = Path("/tmp/test-sync-sms-export-existing.csv")
        csv_path.write_text(
            '"date","message_id","name","email","target_type","target_id","sender_id","direction","to_phone","from_phone","encrypted_text","encrypted_aes_text","mms","timezone"\n'
            '"2026-05-08 03:17:13.418699","6676061264355328","Sales","","department","6500922273529856","6500922273529856","internal","+16694009313","+14155201316","","","","UTC"\n',
            encoding="utf-8",
        )

        with patch("sync_sms_export.init_db", return_value=FakeConn()), \
                patch("sync_sms_export.message_exists", return_value=True), \
                patch("sync_sms_export.store_message") as store_message:
            code, out, err = self._run(
                sync_sms_export,
                ["bin/sync_sms_export.py", "--input-csv", str(csv_path), "--json"],
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_success(parsed, "sync_sms_export.sync")
        self.assertEqual(parsed["data"]["imported"], 0)
        self.assertEqual(parsed["data"]["skipped_existing"], 1)
        store_message.assert_not_called()

    def test_update_contact_argparse_failure_is_json_envelope(self):
        code, out, err = self._run(update_contact, ["bin/update_contact.py", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        parsed = self._parse(out)
        self._assert_error(parsed, "update_contact.update")
        self.assertEqual(parsed["error"]["code"], "invalid_argument")


if __name__ == "__main__":
    unittest.main()
