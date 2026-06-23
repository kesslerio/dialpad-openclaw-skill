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
    class Completed:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout
            self.stderr = ""

    def test_google_calendar_path_wins_when_configured(self):
        events = [
            {
                "summary": "ShapeScale Demo - Synergy Wellness",
                "start": {"dateTime": "2026-06-20T18:15:00Z"},
                "attendees": [{"email": "jane@example.test"}],
            }
        ]
        with patch.object(cal, "GOG_CALENDAR_COMMAND", "/bin/gog-shapescale"), \
                patch.object(cal, "GOG_CALENDAR_ACCOUNT", "martin@shapescale.com"), \
                patch.object(cal, "GOG_CALENDAR_IDS", "primary"), \
                patch("subprocess.run", return_value=self.Completed(json.dumps(events))), \
                patch.object(attio, "_query_records", return_value=[DEAL]) as attio_query:
            ctx = cal.build_calendar_context("Jane jane@example.test Synergy Wellness 2026-06-20T17:00:00Z", now=NOW)
        self.assertTrue(ctx["usable"])
        self.assertEqual(ctx["basis"], "google_calendar")
        self.assertEqual(ctx["startsInMinutes"], 15)
        self.assertIn("ShapeScale Demo", ctx["summary"])
        self.assertIn("(Work)", ctx["summary"])
        attio_query.assert_not_called()

    def test_google_calendar_recent_demo_is_surfaced(self):
        events = [
            {
                "summary": "ShapeScale Demo - Synergy Wellness",
                "start": {"dateTime": "2026-06-20T17:45:00Z"},
            }
        ]
        with patch.object(cal, "GOG_CALENDAR_COMMAND", "/bin/gog-shapescale"), \
                patch.object(cal, "GOG_CALENDAR_IDS", "primary"), \
                patch("subprocess.run", return_value=self.Completed(json.dumps(events))), \
                patch.object(attio, "_query_records", return_value=[]) as attio_query:
            ctx = cal.build_calendar_context("Jane Synergy Wellness 2026-06-20T18:00:00Z", now=NOW)
        self.assertTrue(ctx["usable"])
        self.assertEqual(ctx["basis"], "google_calendar")
        self.assertEqual(ctx["startsInMinutes"], 15)
        self.assertEqual(ctx["demoState"], "recent")
        self.assertIn("Recent demo", ctx["summary"])
        attio_query.assert_not_called()

    def test_google_calendar_queries_recent_lookback_window(self):
        with patch.object(cal, "GOG_CALENDAR_COMMAND", "/bin/gog-shapescale"), \
                patch.object(cal, "GOG_CALENDAR_IDS", "primary"), \
                patch("subprocess.run", return_value=self.Completed(json.dumps([]))) as run, \
                patch.object(attio, "_query_records", return_value=[]):
            cal.build_calendar_context("Jane Synergy Wellness 2026-06-20T18:00:00Z", now=NOW)
        args = run.call_args.args[0]
        self.assertEqual(args[4], "--from")
        self.assertEqual(args[5], "2026-06-13T18:00:00Z")

    def test_google_calendar_checks_sales_team_calendars(self):
        responses = [
            self.Completed(json.dumps([])),
            self.Completed(json.dumps([
                {
                    "summary": "ShapeScale Demo - Synergy Wellness",
                    "start": {"dateTime": "2026-06-20T18:10:00Z"},
                }
            ])),
            self.Completed(json.dumps([])),
        ]
        with patch.object(cal, "GOG_CALENDAR_COMMAND", "/bin/gog-shapescale"), \
                patch.object(cal, "GOG_CALENDAR_IDS", "primary,alex@shapescale.com,lilla@shapescale.com"), \
                patch("subprocess.run", side_effect=responses) as run, \
                patch.object(attio, "_query_records", return_value=[]):
            ctx = cal.build_calendar_context("Jane Synergy Wellness 2026-06-20T17:00:00Z", now=NOW)
        self.assertTrue(ctx["usable"])
        self.assertEqual(ctx["basis"], "google_calendar")
        self.assertEqual(ctx["startsInMinutes"], 10)
        self.assertIn("(Alex)", ctx["summary"])
        queried_calendars = [call.args[0][3] for call in run.call_args_list]
        self.assertEqual(queried_calendars, ["primary", "alex@shapescale.com", "lilla@shapescale.com"])

    def test_google_calendar_availability_returns_compact_windows(self):
        responses = [
            self.Completed(json.dumps([
                {
                    "summary": "Busy internal block",
                    "start": {"dateTime": "2026-06-20T18:00:00Z"},
                    "end": {"dateTime": "2026-06-20T19:00:00Z"},
                    "description": "private notes must not leak",
                }
            ])),
            self.Completed(json.dumps([
                {
                    "summary": "Busy Alex block",
                    "start": {"dateTime": "2026-06-20T19:00:00Z"},
                    "end": {"dateTime": "2026-06-20T20:00:00Z"},
                }
            ])),
            self.Completed(json.dumps([])),
        ]
        with patch.object(cal, "GOG_CALENDAR_COMMAND", "/bin/gog-shapescale"), \
                patch.object(cal, "GOG_CALENDAR_IDS", "primary,alex@shapescale.com,lilla@shapescale.com"), \
                patch.object(cal, "AVAILABILITY_WORKDAY_END_HOUR", 21), \
                patch("subprocess.run", side_effect=responses):
            ctx = cal.build_calendar_context("intent:availability Do you have anything today? Natalie Embody 2026-06-20T18:00:00Z", now=NOW)
        self.assertTrue(ctx["usable"])
        self.assertEqual(ctx["basis"], "google_calendar_availability")
        self.assertEqual(ctx["intent"], "availability")
        self.assertTrue(ctx["candidateWindows"])
        self.assertIn("Candidate windows:", ctx["summary"])
        self.assertNotIn("private notes", json.dumps(ctx).lower())

    def test_google_calendar_availability_missing_command_reports_not_configured(self):
        with patch.object(cal, "GOG_CALENDAR_COMMAND", ""):
            ctx = cal.build_calendar_context("intent:availability anything today", now=NOW)
        self.assertFalse(ctx["usable"])
        self.assertEqual(ctx["status"], "not_configured")
        self.assertEqual(ctx["intent"], "availability")

    def test_google_calendar_availability_uses_safe_label_for_unknown_calendar(self):
        responses = [
            self.Completed(json.dumps([])),
        ]
        with patch.object(cal, "GOG_CALENDAR_COMMAND", "/bin/gog-shapescale"), \
                patch.object(cal, "GOG_CALENDAR_IDS", "private-sales-calendar@example.test"), \
                patch.object(cal, "AVAILABILITY_WORKDAY_END_HOUR", 21), \
                patch("subprocess.run", side_effect=responses):
            ctx = cal.build_calendar_context("intent:availability anything today", now=NOW)
        self.assertTrue(ctx["usable"])
        self.assertIn("Team", ctx["summary"])
        self.assertNotIn("private-sales-calendar", json.dumps(ctx))

    def test_google_calendar_weak_match_falls_through_to_attio(self):
        events = [{"summary": "Unrelated Demo", "start": {"dateTime": "2026-06-20T18:15:00Z"}}]
        with patch.object(cal, "GOG_CALENDAR_COMMAND", "/bin/gog-shapescale"), \
                patch.object(cal, "GOG_CALENDAR_IDS", "primary"), \
                patch("subprocess.run", return_value=self.Completed(json.dumps(events))), \
                patch.object(attio, "_query_records", return_value=[DEAL]):
            ctx = cal.build_calendar_context("Jane Synergy Wellness 2026-06-20T17:00:00Z", now=NOW)
        self.assertTrue(ctx["usable"])
        self.assertEqual(ctx["basis"], "attio")
        self.assertEqual(ctx["startsInMinutes"], 30)

    def test_google_calendar_requires_demo_signal(self):
        events = [
            {
                "summary": "Lunch with Synergy Wellness",
                "start": {"dateTime": "2026-06-20T18:15:00Z"},
                "attendees": [{"email": "jane@example.test"}],
            }
        ]
        with patch.object(cal, "GOG_CALENDAR_COMMAND", "/bin/gog-shapescale"), \
                patch.object(cal, "GOG_CALENDAR_IDS", "primary"), \
                patch("subprocess.run", return_value=self.Completed(json.dumps(events))), \
                patch.object(attio, "_query_records", return_value=[]):
            ctx = cal.build_calendar_context("Jane jane@example.test Synergy Wellness 2026-06-20T17:00:00Z", now=NOW)
        self.assertFalse(ctx["usable"])
        self.assertEqual(ctx["status"], "not_found")

    def test_attio_path_strips_timestamp(self):
        with patch.object(attio, "_query_records", return_value=[DEAL]):
            ctx = cal.build_calendar_context("Jane Synergy Wellness 2026-06-20T17:00:00Z", now=NOW)
        self.assertTrue(ctx["usable"])
        self.assertEqual(ctx["basis"], "attio")
        self.assertEqual(ctx["startsInMinutes"], 30)

    def test_attio_fallback_strips_email_before_token_search(self):
        seen = {}

        def _query(_record_type, query, limit=3):
            seen["token"] = query["name"]["$contains"]
            return [DEAL]

        with patch.object(attio, "_query_records", side_effect=_query):
            ctx = cal.build_calendar_context("Jane jane.longlocal@example.test Synergy 2026-06-20T17:00:00Z", now=NOW)
        self.assertTrue(ctx["usable"])
        self.assertEqual(ctx["basis"], "attio")
        self.assertEqual(seen["token"], "Synergy")

    def test_missing_google_calendar_command_reports_not_configured(self):
        with patch.object(cal, "GOG_CALENDAR_COMMAND", ""), \
                patch.object(attio, "_query_records", return_value=[]), \
                patch.dict("os.environ", {}, clear=True):
            ctx = cal.build_calendar_context("Jane Synergy Wellness 2026-06-20T17:00:00Z", now=NOW)
        self.assertFalse(ctx["usable"])
        self.assertEqual(ctx["status"], "not_configured")

    def test_google_calendar_command_failure_reports_unavailable(self):
        with patch.object(cal, "GOG_CALENDAR_COMMAND", "/missing/gog-shapescale"), \
                patch.object(cal, "GOG_CALENDAR_IDS", "primary"), \
                patch("subprocess.run", side_effect=FileNotFoundError()), \
                patch.object(attio, "_query_records", return_value=[]):
            ctx = cal.build_calendar_context("Jane Synergy Wellness 2026-06-20T17:00:00Z", now=NOW)
        self.assertFalse(ctx["usable"])
        self.assertEqual(ctx["status"], "unavailable")

    def test_empty_query(self):
        ctx = cal.build_calendar_context("", now=NOW)
        self.assertFalse(ctx["usable"])
        self.assertEqual(ctx["status"], "empty_query")

    def test_not_found(self):
        with patch.object(cal, "GOG_CALENDAR_COMMAND", "/bin/gog-shapescale"), \
                patch.object(cal, "GOG_CALENDAR_IDS", "primary"), \
                patch("subprocess.run", return_value=self.Completed(json.dumps([]))), \
                patch.object(attio, "_query_records", return_value=[]):
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
