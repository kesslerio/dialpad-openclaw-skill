"""Tests for scripts/send_gate.py — the gated outbound SMS decision layer."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "bin"))

import send_gate
import send_group_intro
import sms_approval

CONFIG = {
    "shared_lines": {"office": "+14159065785", "sales": "+14155201316"},
    "teammates": {
        "scott": "+14152230323",
        "lilla": "+14158701945",
        "alexandre": "+14153291662",
    },
    "callers": {
        "team-scott": {"shared_lines": ["office", "sales"], "acts_for": "scott"},
        "team-lilla": {"shared_lines": ["office"], "acts_for": "lilla"},
        "operator": {
            "shared_lines": ["office", "sales"],
            "extra_allowed_lines": ["+14155550140"],
            "time_restricted": False,
        },
    },
}

PACIFIC_TO = "+13105550100"   # Los Angeles
EASTERN_TO = "+12125550100"   # New York
MULTIZONE_TO = "+18505550100"  # FL panhandle: Central + Eastern
UNKNOWN_AC_TO = "+18005550199"  # toll-free: not in the NANP table

# A winter moment that is inside the window in both Pacific and Eastern:
# 2026-01-15 17:01 UTC = 09:01 Pacific = 12:01 Eastern, minute :01.
GOOD_UTC = datetime(2026, 1, 15, 17, 1, 0, tzinfo=timezone.utc)


def _sources(company=None, local=None):
    return send_gate.OptOutSources(
        company_opt_outs=company or (lambda: set()),
        local_opt_out=local or (lambda number: False),
    )


def _evaluate(**kwargs):
    kwargs.setdefault("caller", "team-scott")
    kwargs.setdefault("from_number", "+14159065785")
    kwargs.setdefault("to_numbers", [PACIFIC_TO])
    kwargs.setdefault("now_utc", GOOD_UTC)
    kwargs.setdefault("opt_out_sources", _sources())
    kwargs.setdefault("config", CONFIG)
    return send_gate.evaluate_send(**kwargs)


class AllowlistTests(unittest.TestCase):
    def test_shared_lines_allowed(self):
        for line in ("+14159065785", "+14155201316"):
            decision = _evaluate(from_number=line)
            self.assertTrue(decision.allowed, line)

    def test_own_teammate_line_allowed(self):
        decision = _evaluate(from_number="+14152230323")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.acting_for, "scott")

    def test_other_teammate_line_refused(self):
        decision = _evaluate(from_number="+14158701945")
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.retryable)

    def test_shared_line_not_granted_to_caller_refused(self):
        # team-lilla only has the office shared line, not sales.
        decision = _evaluate(caller="team-lilla", from_number="+14155201316")
        self.assertFalse(decision.allowed)

    def test_caller_without_acts_for_cannot_use_teammate_line(self):
        decision = _evaluate(caller="team-lilla", from_number="+14152230323")
        self.assertFalse(decision.allowed)

    def test_unknown_caller_refused(self):
        decision = _evaluate(caller="nobody")
        self.assertFalse(decision.allowed)
        self.assertIn("unknown caller", decision.reasons[0])

    def test_unset_caller_refused(self):
        decision = _evaluate(caller=None)
        self.assertFalse(decision.allowed)

    def test_operator_extra_allowed_line(self):
        decision = _evaluate(caller="operator", from_number="+14155550140")
        self.assertTrue(decision.allowed)

    def test_missing_config_refuses_every_send(self):
        with patch.dict("os.environ", {}, clear=True):
            decision = send_gate.evaluate_from_environment(
                caller="team-scott",
                from_number="+14159065785",
                to_numbers=[PACIFIC_TO],
                opt_out_sources=_sources(),
            )
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.retryable)
        self.assertIn("DIALPAD_SEND_GATE_CONFIG", decision.reasons[0])

    def test_unreadable_config_refuses(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bad = Path(temp_dir) / "gate.json"
            bad.write_text("{not json", encoding="utf-8")
            with patch.dict("os.environ", {"DIALPAD_SEND_GATE_CONFIG": str(bad)}, clear=True):
                decision = send_gate.evaluate_from_environment(
                    caller="team-scott",
                    from_number="+14159065785",
                    to_numbers=[PACIFIC_TO],
                    opt_out_sources=_sources(),
                )
        self.assertFalse(decision.allowed)

    def test_config_rejects_caller_acts_for_unknown_teammate(self):
        bad = {
            **CONFIG,
            "callers": {"x": {"shared_lines": ["office"], "acts_for": "ghost"}},
        }
        with self.assertRaises(send_gate.SendGateError):
            send_gate.validate_config(bad)


class OptOutUnionTests(unittest.TestCase):
    def test_dialpad_company_opt_out_blocks(self):
        decision = _evaluate(
            opt_out_sources=_sources(company=lambda: {"3105550100"}),
        )
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.retryable)
        self.assertIn("dialpad company list", decision.reasons[0])

    def test_local_opt_out_blocks(self):
        decision = _evaluate(
            opt_out_sources=_sources(local=lambda number: True),
        )
        self.assertFalse(decision.allowed)
        self.assertIn("local opt-out", decision.reasons[0])

    def test_dialpad_source_failure_refuses(self):
        def boom():
            raise send_gate.OptOutSourceError("HTTP 500")

        decision = _evaluate(opt_out_sources=_sources(company=boom))
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.retryable)
        self.assertIn("opt-out source unavailable", decision.reasons[0])

    def test_local_source_failure_refuses(self):
        def boom(number):
            raise send_gate.OptOutSourceError("db missing")

        decision = _evaluate(opt_out_sources=_sources(local=boom))
        self.assertFalse(decision.allowed)
        self.assertIn("opt-out source unavailable", decision.reasons[0])

    def test_pagination_is_followed(self):
        pages = [
            {"items": [{"external_endpoint": "+13105550100", "opt_out_state": "opted_out"}], "cursor": "page2"},
            {"items": [{"external_endpoint": "+12125550100", "opt_out_state": "opted_out"}], "cursor": None},
        ]
        calls: list[str] = []

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout=None):
            calls.append(request.full_url)
            return _Resp(pages[len(calls) - 1])

        result = send_gate.fetch_company_opt_outs(
            company_id="5830363354759168",
            api_key="k",
            opener=opener,
        )

        self.assertEqual(len(calls), 2)
        self.assertIn("cursor=page2", calls[1])
        self.assertEqual(result, {"3105550100", "2125550100"})

    def test_fetch_http_error_raises_source_error(self):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, io.BytesIO(b""))

        with self.assertRaises(send_gate.OptOutSourceError):
            send_gate.fetch_company_opt_outs(company_id="1", api_key="k", opener=opener)

    def test_fetch_malformed_body_raises_source_error(self):
        class _Resp:
            def read(self):
                return b"not json"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with self.assertRaises(send_gate.OptOutSourceError):
            send_gate.fetch_company_opt_outs(company_id="1", api_key="k", opener=lambda r, timeout=None: _Resp())

    def test_pagination_repeated_cursor_refuses(self):
        page = {"items": [], "cursor": "same-cursor"}

        class _Resp:
            def read(self):
                return json.dumps(page).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        calls: list[str] = []

        def opener(request, timeout=None):
            calls.append(request.full_url)
            return _Resp()

        with self.assertRaises(send_gate.OptOutSourceError) as ctx:
            send_gate.fetch_company_opt_outs(company_id="1", api_key="k", opener=opener)
        self.assertEqual(len(calls), 2)
        self.assertIn("cursor", str(ctx.exception))

    def test_pagination_page_cap_refuses(self):
        counter = {"n": 0}

        class _Resp:
            def read(self):
                return json.dumps({"items": [], "cursor": f"page-{counter['n']}"}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        calls: list[str] = []

        def opener(request, timeout=None):
            calls.append(request.full_url)
            counter["n"] += 1
            return _Resp()

        with self.assertRaises(send_gate.OptOutSourceError) as ctx:
            send_gate.fetch_company_opt_outs(company_id="1", api_key="k", opener=opener)
        self.assertEqual(len(calls), send_gate._OPT_OUT_MAX_PAGES)
        self.assertIn("pages", str(ctx.exception))


class WindowTests(unittest.TestCase):
    def test_pacific_0759_refused_0801_allowed(self):
        refused = _evaluate(now_utc=datetime(2026, 1, 15, 15, 59, 0, tzinfo=timezone.utc))
        self.assertFalse(refused.allowed)
        allowed = _evaluate(now_utc=GOOD_UTC.replace(hour=16))
        self.assertTrue(allowed.allowed)

    def test_pacific_2059_allowed_2100_refused(self):
        allowed = _evaluate(now_utc=datetime(2026, 1, 16, 4, 59, 0, tzinfo=timezone.utc))
        self.assertTrue(allowed.allowed)
        refused = _evaluate(now_utc=datetime(2026, 1, 16, 5, 1, 0, tzinfo=timezone.utc))
        self.assertFalse(refused.allowed)

    def test_eastern_window_uses_eastern_clock(self):
        # 13:01 UTC = 08:01 EST: inside for Eastern, outside Pacific (05:01 PST).
        moment = datetime(2026, 1, 15, 13, 1, 0, tzinfo=timezone.utc)
        self.assertTrue(_evaluate(now_utc=moment, to_numbers=[EASTERN_TO]).allowed)
        self.assertFalse(_evaluate(now_utc=moment, to_numbers=[PACIFIC_TO]).allowed)

    def test_dst_transitions_shift_the_window(self):
        # July: Pacific is UTC-7 (not -8), so 16:01 UTC is 09:01 PDT.
        self.assertTrue(
            _evaluate(now_utc=datetime(2026, 7, 15, 16, 1, 0, tzinfo=timezone.utc)).allowed
        )
        # ... and 14:01 UTC = 07:01 PDT is outside.
        self.assertFalse(
            _evaluate(now_utc=datetime(2026, 7, 15, 14, 1, 0, tzinfo=timezone.utc)).allowed
        )
        # Eastern summer: 12:01 UTC = 08:01 EDT is inside.
        self.assertTrue(
            _evaluate(
                now_utc=datetime(2026, 7, 15, 12, 1, 0, tzinfo=timezone.utc),
                to_numbers=[EASTERN_TO],
            ).allowed
        )

    def test_multizone_area_code_requires_window_in_all_zones(self):
        # 14:01 UTC winter = 08:01 Central AND 09:01 Eastern: inside both.
        inside = _evaluate(
            now_utc=datetime(2026, 1, 15, 14, 1, 0, tzinfo=timezone.utc),
            to_numbers=[MULTIZONE_TO],
        )
        self.assertTrue(inside.allowed)
        # 13:01 UTC = 07:01 Central but 08:01 Eastern: outside in one zone.
        outside = _evaluate(
            now_utc=datetime(2026, 1, 15, 13, 1, 0, tzinfo=timezone.utc),
            to_numbers=[MULTIZONE_TO],
        )
        self.assertFalse(outside.allowed)

    def test_unknown_area_code_refused(self):
        decision = _evaluate(to_numbers=[UNKNOWN_AC_TO])
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.retryable)
        self.assertIn("unknown", decision.reasons[0].lower())

    def test_window_refusal_is_retryable_with_hint(self):
        decision = _evaluate(now_utc=datetime(2026, 1, 15, 15, 59, 0, tzinfo=timezone.utc))
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.retryable)
        self.assertIsNotNone(decision.retry_after_seconds)


class MinuteRuleTests(unittest.TestCase):
    def test_blocked_minutes_refused(self):
        for minute in (0, 15, 30, 45):
            decision = _evaluate(
                now_utc=datetime(2026, 1, 15, 17, minute, 0, tzinfo=timezone.utc),
            )
            self.assertFalse(decision.allowed, minute)
            self.assertTrue(decision.retryable)
            self.assertIn("minute", decision.reasons[0])

    def test_minute_01_allowed(self):
        decision = _evaluate(now_utc=datetime(2026, 1, 15, 17, 1, 0, tzinfo=timezone.utc))
        self.assertTrue(decision.allowed)

    def test_retry_hint_points_at_next_allowed_minute(self):
        # At :00:30 the next allowed minute boundary is :01 → 30 seconds.
        decision = _evaluate(
            now_utc=datetime(2026, 1, 15, 17, 0, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(decision.retry_after_seconds, 30)
        # Local 07:59:30 is outside the window; the 08:00 candidate lands on a
        # blocked minute, so the hint skips to 08:01 -> 90 seconds.
        decision = _evaluate(
            now_utc=datetime(2026, 1, 15, 15, 59, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(decision.retry_after_seconds, 90)

    def test_newfoundland_half_hour_minute_rule(self):
        # UTC minute :45 -> St_Johns local minute :15: still blocked.
        decision = _evaluate(
            now_utc=datetime(2026, 1, 15, 16, 45, 0, tzinfo=timezone.utc),
            to_numbers=["+17095550100"],
        )
        self.assertFalse(decision.allowed)
        self.assertIn("minute", decision.reasons[0])


class GroupSendTests(unittest.TestCase):
    def test_one_bad_recipient_refuses_whole_send(self):
        decision = _evaluate(to_numbers=[PACIFIC_TO, EASTERN_TO], opt_out_sources=_sources(
            local=lambda number: number == EASTERN_TO,
        ))
        self.assertFalse(decision.allowed)

    def test_all_good_recipients_allowed(self):
        decision = _evaluate(to_numbers=[PACIFIC_TO, EASTERN_TO])
        self.assertTrue(decision.allowed)


class EnvSourceTests(unittest.TestCase):
    def test_missing_company_id_refuses(self):
        sources = send_gate.env_opt_out_sources({"DIALPAD_API_KEY": "k"})
        decision = _evaluate(opt_out_sources=sources)
        self.assertFalse(decision.allowed)
        self.assertIn("DIALPAD_COMPANY_ID", decision.reasons[0])

    def test_missing_api_key_refuses(self):
        sources = send_gate.env_opt_out_sources({"DIALPAD_COMPANY_ID": "1"})
        decision = _evaluate(opt_out_sources=sources)
        self.assertFalse(decision.allowed)
        self.assertIn("DIALPAD_API_KEY", decision.reasons[0])

    def test_missing_approval_db_refuses(self):
        sources = send_gate.env_opt_out_sources(
            {
                "DIALPAD_COMPANY_ID": "1",
                "DIALPAD_API_KEY": "k",
                "DIALPAD_SMS_APPROVAL_DB": "/nonexistent/approvals.db",
            }
        )
        with patch.object(send_gate, "fetch_company_opt_outs", return_value=set()):
            decision = _evaluate(opt_out_sources=sources)
        self.assertFalse(decision.allowed)
        self.assertIn("opt-out db missing", decision.reasons[0])

    def test_local_opt_out_table_blocks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "approvals.db"
            conn = sms_approval.init_db(path=db_path)
            try:
                sms_approval.mark_opt_out(conn, customer_number=PACIFIC_TO)
                conn.commit()
            finally:
                conn.close()
            sources = send_gate.env_opt_out_sources(
                {
                    "DIALPAD_COMPANY_ID": "1",
                    "DIALPAD_API_KEY": "k",
                    "DIALPAD_SMS_APPROVAL_DB": str(db_path),
                }
            )
            with patch.object(send_gate, "fetch_company_opt_outs", return_value=set()):
                blocked = _evaluate(opt_out_sources=sources)
                allowed = _evaluate(to_numbers=[EASTERN_TO], opt_out_sources=sources)
        self.assertFalse(blocked.allowed)
        self.assertIn("local opt-out", blocked.reasons[0])
        self.assertTrue(allowed.allowed)


class WrapperIntegrationTests(unittest.TestCase):
    """bin/send_sms.py must refuse before any provider call and gate dry runs."""

    def _write_config(self, temp_dir: str) -> Path:
        path = Path(temp_dir) / "send-gate.json"
        path.write_text(json.dumps(CONFIG), encoding="utf-8")
        return path

    def _load_wrapper(self):
        # Two modules are named send_sms: bin/send_sms.py (wrapper) and
        # scripts/send_sms.py (direct). Whichever was imported first sits in
        # sys.modules, so pop, import the wrapper explicitly, then restore.
        previous = sys.modules.get("send_sms")
        sys.modules.pop("send_sms", None)
        sys.path.insert(0, str(ROOT / "bin"))
        try:
            import send_sms as wrapper  # noqa: E402
        finally:
            sys.modules.pop("send_sms", None)
            if previous is not None:
                sys.modules["send_sms"] = previous
        return wrapper

    def _run_wrapper(self, wrapper, argv, env_extra=None, sources=None):
        env = {"DIALPAD_SEND_GATE_CONFIG": self.config_path}
        env.update(env_extra or {})
        with patch.dict("os.environ", env, clear=False), \
                patch.object(send_gate, "env_opt_out_sources", return_value=sources or _sources()), \
                patch.object(send_gate, "_utc_now", return_value=GOOD_UTC), \
                patch.object(sys, "argv", argv):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = wrapper.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_path = str(self._write_config(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_wrapper_refuses_before_provider_call(self):
        wrapper = self._load_wrapper()
        with patch.object(wrapper, "require_generated_cli"), \
                patch.object(wrapper, "require_api_key") as require_key, \
                patch.object(wrapper, "run_generated_json") as run_json, \
                patch.object(wrapper, "record_outbound_observation") as observe:
            code, out, err = self._run_wrapper(wrapper, [
                "bin/send_sms.py",
                "--to", PACIFIC_TO,
                "--from", "+14159065785",
                "--caller", "unknown-caller",
                "--message", "hi",
                "--json",
            ])

        self.assertEqual(code, 2)
        require_key.assert_not_called()
        run_json.assert_not_called()
        observe.assert_not_called()
        parsed = json.loads(out)
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["error"]["code"], "send_gate_refused")
        self.assertFalse(parsed["error"]["retryable"])

    def test_wrapper_allows_and_sends_with_caller(self):
        wrapper = self._load_wrapper()
        with patch.object(wrapper, "require_generated_cli"), \
                patch.object(wrapper, "require_api_key"), \
                patch.object(wrapper, "run_generated_json", return_value={"id": "msg-g1", "message_status": "pending"}) as run_json, \
                patch.object(wrapper, "record_outbound_observation", return_value={"memory_sync": "disabled"}), \
                patch.object(wrapper, "drain_on_use"):
            code, out, err = self._run_wrapper(wrapper, [
                "bin/send_sms.py",
                "--to", PACIFIC_TO,
                "--from", "+14152230323",
                "--caller", "team-scott",
                "--message", "hi",
                "--json",
            ])

        self.assertEqual(code, 0)
        run_json.assert_called_once()
        parsed = json.loads(out)
        self.assertTrue(parsed["ok"])

    def test_dry_run_evaluates_gate_without_send_or_log_write(self):
        wrapper = self._load_wrapper()
        with patch.object(wrapper, "require_generated_cli"), \
                patch.object(wrapper, "require_api_key") as require_key, \
                patch.object(wrapper, "run_generated_json") as run_json, \
                patch.object(wrapper, "record_outbound_observation") as observe:
            code, out, err = self._run_wrapper(wrapper, [
                "bin/send_sms.py",
                "--to", PACIFIC_TO,
                "--from", "+14159065785",
                "--caller", "team-scott",
                "--message", "hi",
                "--dry-run",
                "--json",
            ])

        self.assertEqual(code, 0)
        require_key.assert_not_called()
        run_json.assert_not_called()
        observe.assert_not_called()
        parsed = json.loads(out)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["data"]["mode"], "dry_run")
        self.assertTrue(parsed["data"]["send_gate"]["allowed"])
        self.assertEqual(parsed["data"]["send_gate"]["caller"], "team-scott")

    def test_dry_run_refusal_is_nonzero_and_has_reasons(self):
        wrapper = self._load_wrapper()
        with patch.object(wrapper, "require_generated_cli"), \
                patch.object(wrapper, "run_generated_json") as run_json:
            code, out, err = self._run_wrapper(wrapper, [
                "bin/send_sms.py",
                "--to", PACIFIC_TO,
                "--from", "+14159065785",
                "--caller", "team-scott",
                "--message", "hi",
                "--dry-run",
                "--json",
            ], sources=_sources(company=lambda: {"3105550100"}))

        self.assertEqual(code, 2)
        run_json.assert_not_called()
        parsed = json.loads(out)
        self.assertEqual(parsed["error"]["code"], "send_gate_refused")


class ScriptsSendSmsGateTests(unittest.TestCase):
    """scripts/send_sms.py gates main() before POST /sms."""

    def _load_script_module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "scripts_send_sms_gate_test",
            ROOT / "scripts" / "send_sms.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_direct_script_refuses_before_send(self):
        module = self._load_script_module()
        env = {
            "DIALPAD_SEND_GATE_CONFIG": self.config_path,
            "DIALPAD_API_KEY": "k",
        }
        argv = [
            "scripts/send_sms.py",
            "--to", PACIFIC_TO,
            "--from", "+14159065785",
            "--caller", "unknown-caller",
            "--message", "hi",
            "--json",
        ]
        with patch.dict("os.environ", env, clear=False), \
                patch.object(module, "send_sms") as provider_send, \
                patch.object(sys, "argv", argv), \
                self.assertRaises(SystemExit) as ctx, \
                redirect_stdout(io.StringIO()) as stdout, \
                redirect_stderr(io.StringIO()):
            module.main()
        self.assertNotEqual(ctx.exception.code, 0)
        provider_send.assert_not_called()
        parsed = json.loads(stdout.getvalue())
        self.assertEqual(parsed["error"]["code"], "send_gate_refused")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_path = str(Path(self._tmp.name) / "send-gate.json")
        Path(self.config_path).write_text(json.dumps(CONFIG), encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)


class GroupIntroGateTests(unittest.TestCase):
    """bin/send_group_intro.py gates both mirrored recipients before sending."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_path = str(Path(self._tmp.name) / "send-gate.json")
        Path(self.config_path).write_text(json.dumps(CONFIG), encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def _run_intro(self, argv, env_extra=None, sources=None):
        env = {"DIALPAD_SEND_GATE_CONFIG": self.config_path}
        env.update(env_extra or {})
        with patch.dict("os.environ", env, clear=False), \
                patch.object(send_gate, "env_opt_out_sources", return_value=sources or _sources()), \
                patch.object(send_gate, "_utc_now", return_value=GOOD_UTC), \
                patch.object(sys, "argv", argv):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = send_group_intro.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def _argv(self, *extra):
        return [
            "bin/send_group_intro.py",
            "--prospect", PACIFIC_TO,
            "--reference", EASTERN_TO,
            "--from", "+14159065785",
            "--confirm-share",
            *extra,
        ]

    def test_group_intro_refuses_before_any_provider_call(self):
        with patch.object(send_group_intro, "require_generated_cli"), \
                patch.object(send_group_intro, "require_api_key") as require_key, \
                patch.object(send_group_intro, "run_generated_json") as run_json:
            code, out, err = self._run_intro(self._argv("--caller", "unknown-caller", "--json"))

        self.assertEqual(code, 2)
        require_key.assert_not_called()
        run_json.assert_not_called()
        parsed = json.loads(out)
        self.assertEqual(parsed["error"]["code"], "send_gate_refused")
        self.assertFalse(parsed["error"]["retryable"])

    def test_group_intro_one_bad_recipient_refuses_both_sends(self):
        with patch.object(send_group_intro, "require_generated_cli"), \
                patch.object(send_group_intro, "run_generated_json") as run_json:
            code, out, err = self._run_intro(
                self._argv("--caller", "team-scott", "--json"),
                sources=_sources(company=lambda: {"2125550100"}),
            )

        self.assertEqual(code, 2)
        run_json.assert_not_called()
        parsed = json.loads(out)
        self.assertEqual(parsed["error"]["code"], "send_gate_refused")
        self.assertIn(EASTERN_TO, parsed["error"]["message"])

    def test_group_intro_dry_run_reports_gate_decision_without_sending(self):
        with patch.object(send_group_intro, "require_generated_cli"), \
                patch.object(send_group_intro, "require_api_key") as require_key, \
                patch.object(send_group_intro, "run_generated_json") as run_json:
            code, out, err = self._run_intro(self._argv("--caller", "team-scott", "--dry-run", "--json"))

        self.assertEqual(code, 0)
        require_key.assert_not_called()
        run_json.assert_not_called()
        parsed = json.loads(out)
        self.assertTrue(parsed["ok"])
        self.assertTrue(parsed["data"]["send_gate"]["allowed"])
        self.assertEqual(parsed["data"]["send_gate"]["caller"], "team-scott")

    def test_group_intro_caller_defaults_from_env(self):
        with patch.object(send_group_intro, "require_generated_cli"), \
                patch.object(send_group_intro, "require_api_key"), \
                patch.object(
                    send_group_intro, "run_generated_json",
                    return_value={"id": "msg-1", "message_status": "pending"},
                ) as run_json:
            code, out, err = self._run_intro(
                self._argv("--json"),
                env_extra={"DIALPAD_SEND_CALLER": "team-scott"},
            )

        self.assertEqual(code, 0)
        self.assertEqual(run_json.call_count, 2)

    def test_group_intro_rejects_another_teammates_line(self):
        with patch.object(send_group_intro, "require_generated_cli"), \
                patch.object(send_group_intro, "run_generated_json") as run_json:
            code, out, err = self._run_intro([
                "bin/send_group_intro.py",
                "--prospect", PACIFIC_TO,
                "--reference", EASTERN_TO,
                "--from", "+14158701945",
                "--caller", "team-scott",
                "--confirm-share",
                "--json",
            ])

        self.assertEqual(code, 2)
        run_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
