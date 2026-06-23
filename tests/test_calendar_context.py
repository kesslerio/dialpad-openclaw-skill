"""Unit tests for the calendar context adapter (S1/U3). HTTP layer fully mocked."""
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "adapters"))

import attio_context as attio  # noqa: E402
import calendar_context as cal  # noqa: E402

NOW = datetime(2026, 6, 20, 18, 0, 0, tzinfo=timezone.utc)

DEAL = {
    "id": {"record_id": "deal-1"},
    "values": {
        "name": [{"value": "Synergy Wellness (Inbound Demo Request)", "attribute_type": "text"}],
        "demo_scheduled_at": [{"value": "2026-06-20T18:30:00.000000000Z", "attribute_type": "timestamp"}],
    },
}
PAST_DEAL = {
    "id": {"record_id": "deal-2"},
    "values": {
        "name": [{"value": "Old Deal", "attribute_type": "text"}],
        "demo_scheduled_at": [{"value": "2026-06-20T17:30:00.000000000Z", "attribute_type": "timestamp"}],
    },
}


class TimeHelpersTests(unittest.TestCase):
    def test_parse_iso_trims_nanoseconds(self):
        dt = cal.parse_iso("2026-06-20T18:30:00.000000000Z")
        self.assertEqual(dt, datetime(2026, 6, 20, 18, 30, tzinfo=timezone.utc))

    def test_parse_iso_date_only(self):
        self.assertEqual(cal.parse_iso("2026-06-20"), datetime(2026, 6, 20, tzinfo=timezone.utc))

    def test_parse_iso_invalid(self):
        self.assertIsNone(cal.parse_iso("not-a-date"))

    def test_starts_in_minutes(self):
        self.assertEqual(cal.starts_in_minutes(datetime(2026, 6, 20, 18, 30, tzinfo=timezone.utc), now=NOW), 30)
        self.assertIsNone(cal.starts_in_minutes(None, now=NOW))

    def test_search_token_picks_longest(self):
        self.assertEqual(cal._search_token("John Doe Acme Synergy"), "Synergy")
        self.assertIsNone(cal._search_token("a b c"))


class ResolveAttioDemoTests(unittest.TestCase):
    def test_single_future_match(self):
        with patch.object(attio, "_query_records", return_value=[DEAL]):
            sim, summary, state = cal.resolve_attio_demo("Synergy Wellness", now=NOW)
        self.assertEqual(sim, 30)
        self.assertIn("Synergy Wellness", summary)
        self.assertEqual(state, "upcoming")

    def test_ambiguous_match_not_usable(self):
        with patch.object(attio, "_query_records", return_value=[DEAL, PAST_DEAL]):
            self.assertEqual(cal.resolve_attio_demo("Demo", now=NOW), (None, None, None))

    def test_no_match(self):
        with patch.object(attio, "_query_records", return_value=[]):
            self.assertEqual(cal.resolve_attio_demo("Synergy", now=NOW), (None, None, None))

    def test_recent_past_demo_is_surfaced(self):
        with patch.object(attio, "_query_records", return_value=[PAST_DEAL]):
            sim, summary, state = cal.resolve_attio_demo("Old Deal", now=NOW)
        self.assertGreater(sim, 0)
        self.assertIn("Recent demo", summary)
        self.assertEqual(state, "recent")

    def test_stale_past_demo_not_surfaced(self):
        stale = {
            "id": {"record_id": "deal-3"},
            "values": {
                "name": [{"value": "Stale Deal", "attribute_type": "text"}],
                "demo_scheduled_at": [{"value": "2026-06-01T10:00:00.000000000Z", "attribute_type": "timestamp"}],
            },
        }
        with patch.object(attio, "_query_records", return_value=[stale]):
            self.assertEqual(cal.resolve_attio_demo("Stale", now=NOW), (None, None, None))

    def test_api_error_fails_closed(self):
        with patch.object(attio, "_query_records", side_effect=attio.AttioError("network")):
            self.assertEqual(cal.resolve_attio_demo("Synergy", now=NOW), (None, None, None))


class BuildCalendarContextTests(unittest.TestCase):
    def test_attio_path_strips_timestamp(self):
        with patch.object(attio, "_query_records", return_value=[DEAL]):
            ctx = cal.build_calendar_context("Jane Synergy Wellness 2026-06-20T17:00:00Z", now=NOW)
        self.assertTrue(ctx["usable"])
        self.assertEqual(ctx["basis"], "attio")
        self.assertEqual(ctx["startsInMinutes"], 30)

    def test_empty_query(self):
        ctx = cal.build_calendar_context("", now=NOW)
        self.assertFalse(ctx["usable"])
        self.assertEqual(ctx["status"], "empty_query")

    def test_not_found(self):
        with patch.object(attio, "_query_records", return_value=[]):
            ctx = cal.build_calendar_context("Nobody Unknown", now=NOW)
        self.assertFalse(ctx["usable"])
        self.assertEqual(ctx["status"], "not_found")


class CalendlyBestEffortTests(unittest.TestCase):
    def _fake_urlopen(self, payloads):
        class FakeResp:
            def __init__(self, payload):
                self._p = json.dumps(payload).encode("utf-8")

            def read(self):
                return self._p

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else req
            if "/users/me" in url:
                return FakeResp(payloads["me"])
            return FakeResp(payloads["events"])

        return opener

    def test_calendly_next_event(self):
        payloads = {
            "me": {"resource": {"current_organization": "https://api.calendly.com/organizations/ORG"}},
            "events": {"collection": [{"name": "ShapeScale Demo", "start_time": "2026-06-20T18:45:00Z"}]},
        }
        with patch.dict("os.environ", {"CALENDLY_API_KEY": "tok"}), \
             patch("urllib.request.urlopen", side_effect=self._fake_urlopen(payloads)):
            sim, summary = cal.calendly_next_event("invitee@example.com", now=NOW)
        self.assertEqual(sim, 45)
        self.assertIn("ShapeScale Demo", summary)

    def test_calendly_disabled_without_key(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(cal.calendly_next_event("invitee@example.com", now=NOW), (None, None))

    def test_build_falls_back_to_calendly_when_email_present(self):
        payloads = {
            "me": {"resource": {"current_organization": "https://api.calendly.com/organizations/ORG"}},
            "events": {"collection": [{"name": "Demo", "start_time": "2026-06-20T18:20:00Z"}]},
        }
        with patch.object(attio, "_query_records", return_value=[]), \
             patch.dict("os.environ", {"CALENDLY_API_KEY": "tok"}), \
             patch("urllib.request.urlopen", side_effect=self._fake_urlopen(payloads)):
            ctx = cal.build_calendar_context("Jane invitee@example.com 2026-06-20T17:00:00Z", now=NOW)
        self.assertTrue(ctx["usable"])
        self.assertEqual(ctx["basis"], "calendly")
        self.assertEqual(ctx["startsInMinutes"], 20)
        self.assertEqual(ctx["demoState"], "upcoming")


class HardeningTests(unittest.TestCase):
    def test_parse_int_env_falls_back_on_invalid_value(self):
        with patch.dict("os.environ", {"BAD_INT": "not-an-int"}):
            self.assertEqual(cal.parse_int_env("BAD_INT", 42), 42)

    def test_main_exits_zero_on_internal_exception(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with patch.object(cal, "build_calendar_context", side_effect=RuntimeError("boom")), redirect_stdout(buf):
            rc = cal.main(["anything"])
        self.assertEqual(rc, 0)
        self.assertFalse(json.loads(buf.getvalue())["usable"])

    def test_event_name_sanitized(self):
        deal = {
            "id": {"record_id": "d"},
            "values": {
                "name": [{"value": "Evil\nDemo\x00", "attribute_type": "text"}],
                "demo_scheduled_at": [{"value": "2026-06-20T18:30:00.000000000Z"}],
            },
        }
        with patch.object(attio, "_query_records", return_value=[deal]):
            sim, summary, _state = cal.resolve_attio_demo("Evil", now=NOW)
        self.assertEqual(summary, "Upcoming demo: Evil Demo")

    def test_calendly_rejects_unexpected_org(self):
        payloads = {
            "me": {"resource": {"current_organization": "https://evil.example/org"}},
            "events": {"collection": [{"name": "x", "start_time": "2026-06-20T18:45:00Z"}]},
        }
        with patch.dict("os.environ", {"CALENDLY_API_KEY": "tok"}), \
             patch("urllib.request.urlopen", side_effect=self_fake_urlopen(payloads)):
            self.assertEqual(cal.calendly_next_event("a@b.com", now=NOW), (None, None))


def self_fake_urlopen(payloads):
    class FakeResp:
        def __init__(self, payload):
            self._p = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._p

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        return FakeResp(payloads["me"] if "/users/me" in url else payloads["events"])

    return opener


if __name__ == "__main__":
    unittest.main()
