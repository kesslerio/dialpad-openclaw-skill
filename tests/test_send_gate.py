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
from _dialpad_compat import WrapperError
from nanp_area_code_timezones import timezones_for_e164

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

    def test_from_number_sharing_last10_with_approved_line_refused(self):
        # normalize_phone_number-style last-10 matching would collide this
        # non-Dialpad line with the office line; strict E.164 must not.
        decision = _evaluate(from_number="+9994159065785")
        self.assertFalse(decision.allowed)
        self.assertIn("not an approved line", decision.reasons[0])

    def test_bare_10_digit_national_form_allowed(self):
        decision = _evaluate(from_number="4159065785")
        self.assertTrue(decision.allowed)

    def test_1_prefixed_national_form_allowed(self):
        decision = _evaluate(from_number="14159065785")
        self.assertTrue(decision.allowed)


class MalformedConfigTests(unittest.TestCase):
    def test_shared_lines_null_raises_send_gate_error(self):
        bad = {
            **CONFIG,
            "callers": {"team-scott": {"shared_lines": None}},
        }
        with self.assertRaises(send_gate.SendGateError):
            send_gate.validate_config(bad)

    def test_shared_lines_non_string_element_raises_send_gate_error(self):
        bad = {
            **CONFIG,
            "callers": {"team-scott": {"shared_lines": [["office"]]}},
        }
        with self.assertRaises(send_gate.SendGateError):
            send_gate.validate_config(bad)

    def test_extra_allowed_lines_null_raises_send_gate_error(self):
        bad = {
            **CONFIG,
            "callers": {"x": {"shared_lines": ["office"], "extra_allowed_lines": None}},
        }
        with self.assertRaises(send_gate.SendGateError):
            send_gate.validate_config(bad)

    def test_acts_for_non_string_raises_send_gate_error(self):
        bad = {
            **CONFIG,
            "callers": {"x": {"shared_lines": ["office"], "acts_for": ["scott"]}},
        }
        with self.assertRaises(send_gate.SendGateError):
            send_gate.validate_config(bad)

    def test_unexpected_config_load_error_refuses(self):
        with patch.object(send_gate, "load_config", side_effect=TypeError("boom")), \
                patch.dict("os.environ", {"DIALPAD_SEND_GATE_CONFIG": "/nonexistent/gate.json"}, clear=True):
            decision = send_gate.evaluate_from_environment(
                caller="team-scott",
                from_number="+14159065785",
                to_numbers=[PACIFIC_TO],
                opt_out_sources=_sources(),
            )
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.retryable)
        self.assertIn("config unavailable", decision.reasons[0])


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

    def _fetch_with_payload(self, payload):
        class _Resp:
            def read(self):
                return json.dumps(payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return send_gate.fetch_company_opt_outs(
            company_id="1",
            api_key="k",
            opener=lambda request, timeout=None: _Resp(),
        )

    def test_body_without_items_list_is_malformed(self):
        with self.assertRaises(send_gate.OptOutSourceError):
            self._fetch_with_payload({})

    def test_items_null_without_cursor_is_empty(self):
        self.assertEqual(self._fetch_with_payload({"items": None}), set())

    def test_items_null_with_cursor_is_malformed(self):
        with self.assertRaises(send_gate.OptOutSourceError):
            self._fetch_with_payload({"items": None, "cursor": "next"})

    def test_items_non_list_is_malformed(self):
        with self.assertRaises(send_gate.OptOutSourceError):
            self._fetch_with_payload({"items": "nope"})

    def test_item_missing_opt_out_state_is_malformed(self):
        with self.assertRaises(send_gate.OptOutSourceError):
            self._fetch_with_payload({"items": [{"external_endpoint": "+13105550100"}]})

    def test_item_missing_external_endpoint_is_malformed(self):
        with self.assertRaises(send_gate.OptOutSourceError):
            self._fetch_with_payload({"items": [{"opt_out_state": "opted_out"}]})

    def test_non_opted_out_items_are_skipped(self):
        result = self._fetch_with_payload(
            {
                "items": [
                    {"external_endpoint": "+12125550100", "opt_out_state": "opted_in"},
                    {"external_endpoint": "+13105550100", "opt_out_state": "opted_out"},
                ]
            }
        )
        self.assertEqual(result, {"3105550100"})

    def test_explicit_empty_items_is_valid(self):
        self.assertEqual(self._fetch_with_payload({"items": []}), set())

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

    def test_mixed_hard_and_time_refusal_is_not_retryable(self):
        # Company opt-out on one recipient + outside-window on another: the
        # refusal is not retryable and must not advertise a retry hint.
        decision = _evaluate(
            to_numbers=[PACIFIC_TO, EASTERN_TO],
            opt_out_sources=_sources(company=lambda: {"3105550100"}),
            now_utc=datetime(2026, 1, 15, 12, 31, tzinfo=timezone.utc),
        )
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.retryable)
        self.assertIsNone(decision.retry_after_seconds)
        self.assertNotIn("retry_after_seconds", decision.public())


class AreaCodeTableTests(unittest.TestCase):
    def test_807_spans_eastern_and_central(self):
        zones = {zone.key for zone in timezones_for_e164("+18075550100")}
        self.assertEqual(zones, {"America/Toronto", "America/Winnipeg"})

    def test_807_kenora_refused_before_0800_central(self):
        # 13:31 UTC = 08:31 EST (Thunder Bay) but 07:31 CST (Kenora).
        decision = _evaluate(
            to_numbers=["+18075550100"],
            now_utc=datetime(2026, 1, 15, 13, 31, tzinfo=timezone.utc),
        )
        self.assertFalse(decision.allowed)

    def test_334_spans_central_and_eastern(self):
        zones = {zone.key for zone in timezones_for_e164("+13345550100")}
        self.assertEqual(zones, {"America/Chicago", "America/New_York"})

    def test_334_phenix_city_refused_after_2100_eastern(self):
        # 2026-01-16 02:31 UTC = 20:31 CST but 21:31 EST (Phenix City).
        decision = _evaluate(
            to_numbers=["+13345550100"],
            now_utc=datetime(2026, 1, 16, 2, 31, tzinfo=timezone.utc),
        )
        self.assertFalse(decision.allowed)

    def test_324_jacksonville_overlay_is_eastern_only(self):
        zones = [zone.key for zone in timezones_for_e164("+13245550100")]
        self.assertEqual(zones, ["America/New_York"])
        # 13:31 UTC = 08:31 EST: inside the window; a phantom Central zone
        # would refuse this at 07:31.
        decision = _evaluate(
            to_numbers=["+13245550100"],
            now_utc=datetime(2026, 1, 15, 13, 31, tzinfo=timezone.utc),
        )
        self.assertTrue(decision.allowed)

    def test_656_tampa_is_known_eastern(self):
        zones = [zone.key for zone in timezones_for_e164("+16565550100")]
        self.assertEqual(zones, ["America/New_York"])
        decision = _evaluate(to_numbers=["+16565550100"], now_utc=GOOD_UTC)
        self.assertTrue(decision.allowed)

    def test_483_alabama_overlay_zones(self):
        zones = {zone.key for zone in timezones_for_e164("+14835550100")}
        self.assertEqual(zones, {"America/Chicago", "America/New_York"})
        decision = _evaluate(to_numbers=["+14835550100"], now_utc=GOOD_UTC)
        self.assertTrue(decision.allowed)


class ShippedExampleConfigTests(unittest.TestCase):
    """references/send-gate.example.json is the decided caller-to-line
    mapping: it must load and validate as shipped."""

    @classmethod
    def setUpClass(cls):
        cls.config = send_gate.load_config(ROOT / "references" / "send-gate.example.json")

    def _eval(self, caller, from_number, now_utc=GOOD_UTC):
        return send_gate.evaluate_send(
            caller=caller,
            from_number=from_number,
            to_numbers=[PACIFIC_TO],
            now_utc=now_utc,
            opt_out_sources=_sources(),
            config=self.config,
        )

    def test_example_config_loads_and_validates(self):
        config = send_gate.load_config(ROOT / "references" / "send-gate.example.json")
        self.assertEqual(config["shared_lines"]["office"], "+14159065785")
        self.assertEqual(config["shared_lines"]["sales"], "+14155201316")
        self.assertEqual(
            set(config["callers"]),
            {"team-scott", "team-lilla", "team-alexandre", "operator"},
        )

    def test_example_team_callers_use_shared_and_own_lines(self):
        own_lines = {
            "team-scott": "+14152230323",
            "team-lilla": "+14158701945",
            "team-alexandre": "+14153291662",
        }
        for caller, own_line in own_lines.items():
            for line in (own_line, "+14159065785", "+14155201316"):
                with self.subTest(caller=caller, line=line):
                    self.assertTrue(self._eval(caller, line).allowed)

    def test_example_team_callers_refuse_other_teammate_and_extra_lines(self):
        own_lines = {
            "team-scott": "+14152230323",
            "team-lilla": "+14158701945",
            "team-alexandre": "+14153291662",
        }
        for caller, own_line in own_lines.items():
            refused = [line for line in own_lines.values() if line != own_line]
            refused.append("+14153602954")
            for line in refused:
                with self.subTest(caller=caller, line=line):
                    decision = self._eval(caller, line)
                    self.assertFalse(decision.allowed)
                    self.assertFalse(decision.retryable)

    def test_example_operator_uses_shared_and_extra_lines(self):
        for line in ("+14159065785", "+14155201316", "+14153602954"):
            with self.subTest(line=line):
                self.assertTrue(self._eval("operator", line).allowed)

    def test_example_operator_refused_teammate_lines(self):
        for line in ("+14152230323", "+14158701945", "+14153291662"):
            with self.subTest(line=line):
                self.assertFalse(self._eval("operator", line).allowed)

    def test_example_operator_stays_time_restricted(self):
        # The decided defaults omit time_restricted, so the window and minute
        # rules apply to the operator caller too.
        self.assertNotIn("time_restricted", self.config["callers"]["operator"])
        # 2026-01-16 05:01 UTC = 21:01 Pacific: outside the window.
        outside = self._eval(
            "operator",
            "+14159065785",
            now_utc=datetime(2026, 1, 16, 5, 1, 0, tzinfo=timezone.utc),
        )
        self.assertFalse(outside.allowed)
        self.assertTrue(outside.retryable)
        # Minute :15 is blocked even inside the window.
        at_15 = self._eval(
            "operator",
            "+14159065785",
            now_utc=datetime(2026, 1, 15, 17, 15, 0, tzinfo=timezone.utc),
        )
        self.assertFalse(at_15.allowed)
        self.assertTrue(at_15.retryable)
        self.assertIn("minute", at_15.reasons[0])


class ApprovalSendGateTests(unittest.TestCase):
    """Approval-lane caller resolution and the evaluate_approved_send wrapper."""

    def _config_file(self, config):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        try:
            json.dump(config, tmp)
        finally:
            tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def test_approval_caller_prefers_dialpad_send_caller(self):
        env = {"DIALPAD_SEND_CALLER": " team-lilla "}
        self.assertEqual(send_gate.approval_caller_from_env(env), "team-lilla")

    def test_approval_caller_falls_back_to_operator_only_when_defined(self):
        with_operator = self._config_file(CONFIG)
        env = {"DIALPAD_SEND_GATE_CONFIG": with_operator}
        self.assertEqual(send_gate.approval_caller_from_env(env), "operator")

        no_operator = {
            **CONFIG,
            "callers": {"team-scott": CONFIG["callers"]["team-scott"]},
        }
        without_operator = self._config_file(no_operator)
        env = {"DIALPAD_SEND_GATE_CONFIG": without_operator}
        self.assertIsNone(send_gate.approval_caller_from_env(env))

    def test_approval_caller_unset_without_config(self):
        self.assertIsNone(send_gate.approval_caller_from_env({}))

    def test_evaluate_approved_send_resolves_operator_fallback(self):
        path = self._config_file(CONFIG)
        env = {"DIALPAD_SEND_GATE_CONFIG": path}
        decision = send_gate.evaluate_approved_send(
            from_number="+14155550140",
            to_numbers=[PACIFIC_TO],
            now_utc=GOOD_UTC,
            opt_out_sources=_sources(),
            env=env,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.caller, "operator")

    def test_evaluate_approved_send_env_caller_wins(self):
        path = self._config_file(CONFIG)
        env = {"DIALPAD_SEND_GATE_CONFIG": path, "DIALPAD_SEND_CALLER": "team-scott"}
        decision = send_gate.evaluate_approved_send(
            from_number="+14152230323",
            to_numbers=[PACIFIC_TO],
            now_utc=GOOD_UTC,
            opt_out_sources=_sources(),
            env=env,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.caller, "team-scott")

    def test_evaluate_approved_send_refuses_unapproved_line(self):
        path = self._config_file(CONFIG)
        env = {"DIALPAD_SEND_GATE_CONFIG": path}
        decision = send_gate.evaluate_approved_send(
            from_number="+14158701945",
            to_numbers=[PACIFIC_TO],
            now_utc=GOOD_UTC,
            opt_out_sources=_sources(),
            env=env,
        )
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.retryable)


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


class EmergencyOptOutSourceTests(unittest.TestCase):
    """The gate's local source reads emergency markers strictly: an existing
    but unreadable or malformed marker fails closed (unlike the best-effort
    is_emergency_opted_out used elsewhere)."""

    def setUp(self):
        sms_approval._EMERGENCY_OPT_OUT_MEMORY.clear()
        self.addCleanup(sms_approval._EMERGENCY_OPT_OUT_MEMORY.clear)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "approvals.db"
        conn = sms_approval.init_db(path=self.db)
        conn.close()
        self.config_path = Path(self._tmp.name) / "send-gate.json"
        self.config_path.write_text(json.dumps(CONFIG), encoding="utf-8")

    def _env(self, marker):
        return {
            "DIALPAD_SEND_GATE_CONFIG": str(self.config_path),
            "DIALPAD_COMPANY_ID": "1",
            "DIALPAD_API_KEY": "k",
            "DIALPAD_SMS_APPROVAL_DB": str(self.db),
            "DIALPAD_SMS_APPROVAL_EMERGENCY_PATH": str(marker),
        }

    def _evaluate_env(self, env):
        with patch.dict("os.environ", env, clear=False), \
                patch.object(send_gate, "fetch_company_opt_outs", return_value=set()):
            return send_gate.evaluate_from_environment(
                caller="team-scott",
                from_number="+14159065785",
                to_numbers=[PACIFIC_TO],
                now_utc=GOOD_UTC,
                env=env,
            )

    def test_marker_path_is_a_directory_refuses(self):
        marker = Path(self._tmp.name) / "emergency-dir"
        marker.mkdir()
        decision = self._evaluate_env(self._env(marker))
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.retryable)
        self.assertIn("opt-out source unavailable", decision.reasons[0])

    def test_marker_corrupt_line_refuses(self):
        marker = Path(self._tmp.name) / "emergency.jsonl"
        marker.write_text(
            json.dumps({"customer_number_normalized": "2125550100"}) + "\n"
            + "{not json\n",
            encoding="utf-8",
        )
        decision = self._evaluate_env(self._env(marker))
        self.assertFalse(decision.allowed)
        self.assertIn("opt-out source unavailable", decision.reasons[0])

    def test_marker_opted_out_recipient_refuses(self):
        marker = Path(self._tmp.name) / "emergency.jsonl"
        marker.write_text(
            json.dumps({"customer_number_normalized": "3105550100"}) + "\n",
            encoding="utf-8",
        )
        decision = self._evaluate_env(self._env(marker))
        self.assertFalse(decision.allowed)
        self.assertIn("local opt-out", decision.reasons[0])

    def test_missing_marker_is_fine(self):
        marker = Path(self._tmp.name) / "does-not-exist.jsonl"
        decision = self._evaluate_env(self._env(marker))
        self.assertTrue(decision.allowed)

    def test_missing_approval_db_refuses_every_send(self):
        env = self._env(Path(self._tmp.name) / "absent-marker.jsonl")
        env["DIALPAD_SMS_APPROVAL_DB"] = str(Path(self._tmp.name) / "does-not-exist.db")
        decision = self._evaluate_env(env)
        self.assertFalse(decision.allowed)
        self.assertIn("opt-out", decision.reasons[0])


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
        ledger = Path(self._tmp.name) / "receipts.jsonl"
        with patch.object(wrapper, "require_generated_cli"), \
                patch.object(wrapper, "require_api_key") as require_key, \
                patch.object(wrapper, "run_generated_json") as run_json, \
                patch.object(wrapper, "record_outbound_observation") as observe, \
                patch.object(wrapper, "append_sms_send_receipt") as receipt:
            code, out, err = self._run_wrapper(
                wrapper,
                [
                    "bin/send_sms.py",
                    "--to", PACIFIC_TO,
                    "--from", "+14159065785",
                    "--caller", "team-scott",
                    "--message", "hi",
                    "--dry-run",
                    "--json",
                ],
                env_extra={"DIALPAD_SMS_RECEIPT_LEDGER": str(ledger)},
            )

        self.assertEqual(code, 0)
        require_key.assert_not_called()
        run_json.assert_not_called()
        observe.assert_not_called()
        receipt.assert_not_called()
        self.assertFalse(ledger.exists())
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

    def test_direct_script_dry_run_json_refusal_carries_error_code_and_report(self):
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
            "--dry-run",
            "--json",
        ]
        with patch.dict("os.environ", env, clear=False), \
                patch.object(module, "send_sms") as provider_send, \
                patch.object(sys, "argv", argv), \
                self.assertRaises(SystemExit) as ctx, \
                redirect_stdout(io.StringIO()) as stdout, \
                redirect_stderr(io.StringIO()):
            module.main()
        self.assertEqual(ctx.exception.code, 2)
        provider_send.assert_not_called()
        parsed = json.loads(stdout.getvalue())
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["error"]["code"], "send_gate_refused")
        self.assertEqual(parsed["mode"], "dry_run")
        self.assertIn("send_gate", parsed)

    def test_direct_script_from_is_required(self):
        module = self._load_script_module()
        argv = [
            "scripts/send_sms.py",
            "--to", PACIFIC_TO,
            "--caller", "team-scott",
            "--message", "hi",
        ]
        with patch.object(sys, "argv", argv), \
                self.assertRaises(SystemExit) as ctx, \
                redirect_stderr(io.StringIO()):
            module.main()
        self.assertEqual(ctx.exception.code, 2)

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

    def test_group_intro_records_each_confirmed_send_to_shared_log(self):
        with patch.object(send_group_intro, "require_generated_cli"), \
                patch.object(send_group_intro, "require_api_key"), \
                patch.object(
                    send_group_intro,
                    "record_outbound_observation",
                    return_value={"memory_sync": "disabled"},
                ) as observe, \
                patch.object(
                    send_group_intro,
                    "run_generated_json",
                    side_effect=[
                        {"id": "msg-p", "message_status": "pending"},
                        {"id": "msg-r", "message_status": "pending"},
                    ],
                ) as run_json:
            code, out, err = self._run_intro(self._argv("--caller", "team-scott", "--json"))

        self.assertEqual(code, 0)
        self.assertEqual(run_json.call_count, 2)
        self.assertEqual(observe.call_count, 2)
        calls = observe.call_args_list
        self.assertEqual(calls[0].kwargs["to_numbers"], [PACIFIC_TO])
        self.assertEqual(calls[1].kwargs["to_numbers"], [EASTERN_TO])
        self.assertTrue(all(c.kwargs["from_number"] == "+14159065785" for c in calls))

    def test_group_intro_partial_success_records_confirmed_leg_only(self):
        with patch.object(send_group_intro, "require_generated_cli"), \
                patch.object(send_group_intro, "require_api_key"), \
                patch.object(
                    send_group_intro,
                    "record_outbound_observation",
                    return_value={"memory_sync": "disabled"},
                ) as observe, \
                patch.object(
                    send_group_intro,
                    "run_generated_json",
                    side_effect=[
                        {"id": "msg-p", "message_status": "pending"},
                        WrapperError("provider boom", code="upstream_error", retryable=True),
                    ],
                ):
            code, out, err = self._run_intro(self._argv("--caller", "team-scott", "--json"))

        self.assertEqual(code, 2)
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(observe.call_args_list[0].kwargs["to_numbers"], [PACIFIC_TO])
        parsed = json.loads(out)
        self.assertEqual(parsed["error"]["code"], "partial_success")

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
