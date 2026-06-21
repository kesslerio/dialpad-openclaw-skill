"""Tests for the read-only SMS approval eval + weekly pulse (S6).

These tests use temporary databases only; they never touch live data.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sms_approval_eval as ev  # noqa: E402


def _make_approval_db(path: str, rows: list[dict]) -> None:
    """Create an approval DB matching the live schema and insert rows."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE sms_approval_drafts (
            draft_id TEXT PRIMARY KEY,
            thread_key TEXT,
            customer_number TEXT,
            sender_number TEXT,
            draft_text TEXT,
            risk_state TEXT,
            status TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            invalidated_reason TEXT,
            dialpad_sms_id TEXT,
            metadata_json TEXT
        )
        """
    )
    for i, r in enumerate(rows):
        conn.execute(
            """
            INSERT INTO sms_approval_drafts (
                draft_id, thread_key, customer_number, sender_number, draft_text,
                risk_state, status, created_at_ms, invalidated_reason,
                dialpad_sms_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.get("draft_id", f"draft_{i}"),
                r.get("thread_key", "t"),
                r.get("customer_number", "+14155550000"),
                r.get("sender_number", "+14155201316"),
                r.get("draft_text", "draft body"),
                r.get("risk_state", "normal"),
                r["status"],
                r.get("created_at_ms", 1_000_000),
                r.get("invalidated_reason"),
                r.get("dialpad_sms_id"),
                r.get("metadata_json"),
            ),
        )
    conn.commit()
    conn.close()


class VerdictClassificationTests(unittest.TestCase):
    def test_sent_is_accept(self):
        self.assertEqual(ev.classify_verdict("sent", None), ev.VERDICT_ACCEPT)

    def test_failed_is_accept(self):
        # Operator approved; delivery failure is a separate axis.
        self.assertEqual(ev.classify_verdict("failed", None), ev.VERDICT_ACCEPT)

    def test_rejected_is_reject(self):
        self.assertEqual(ev.classify_verdict("rejected", "operator_rejected"), ev.VERDICT_REJECT)

    def test_manual_outbound_is_reject(self):
        # MAINTAINER DECISION: hand-typed reply == draft rejection, not accept.
        self.assertEqual(ev.classify_verdict("stale", "manual_outbound"), ev.VERDICT_REJECT)

    def test_superseded_is_excluded(self):
        # Customer texted again before operator acted -> not a verdict.
        self.assertEqual(ev.classify_verdict("stale", "superseded_by_new_draft"), ev.VERDICT_EXCLUDED)

    def test_pending_is_excluded(self):
        self.assertEqual(ev.classify_verdict("pending", None), ev.VERDICT_EXCLUDED)
        self.assertEqual(ev.classify_verdict("risk_pending", None), ev.VERDICT_EXCLUDED)
        self.assertEqual(ev.classify_verdict("sending", None), ev.VERDICT_EXCLUDED)

    def test_stale_unknown_reason_is_excluded_not_silently_counted(self):
        self.assertEqual(ev.classify_verdict("stale", "some_new_reason"), ev.VERDICT_EXCLUDED)
        self.assertEqual(ev.classify_verdict("stale", None), ev.VERDICT_EXCLUDED)


class ExhaustivePartitionTests(unittest.TestCase):
    def test_all_known_statuses_pass(self):
        rows = [{"status": s} for s in ev.ALL_STATUSES]
        histogram = ev.assert_exhaustive_partition(rows)
        self.assertEqual(sum(histogram.values()), len(ev.ALL_STATUSES))

    def test_unknown_status_raises(self):
        rows = [{"status": "sent"}, {"status": "brand_new_status"}]
        with self.assertRaises(ValueError):
            ev.assert_exhaustive_partition(rows)

    def test_taxonomy_covers_sms_approval_constants(self):
        # Guard against drift: every STATUS_* in sms_approval must be in ALL_STATUSES.
        import sms_approval as sa

        status_consts = {
            v for k, v in vars(sa).items() if k.startswith("STATUS_") and isinstance(v, str)
        }
        self.assertEqual(status_consts, set(ev.ALL_STATUSES))

    def test_partition_sum_equals_total_rows(self):
        # The pulse's core invariant: sum of buckets == total rows.
        rows = [
            {"status": "sent", "invalidated_reason": None},
            {"status": "rejected", "invalidated_reason": "operator_rejected"},
            {"status": "stale", "invalidated_reason": "manual_outbound"},
            {"status": "stale", "invalidated_reason": "superseded_by_new_draft"},
            {"status": "pending", "invalidated_reason": None},
        ]
        stats = ev.aggregate_by_category(rows)
        total = sum(c.total for c in stats.values())
        self.assertEqual(total, len(rows))


class CategoryAggregationTests(unittest.TestCase):
    def test_per_category_accept_reject_rates(self):
        sales_meta = json.dumps({"line_display": "Sales (415) 520-1316"})
        support_meta = json.dumps({"line_display": "Support (415) 999-0000"})
        rows = [
            {"status": "sent", "metadata_json": sales_meta},
            {"status": "sent", "metadata_json": sales_meta},
            {"status": "stale", "invalidated_reason": "manual_outbound", "metadata_json": sales_meta},
            {"status": "rejected", "invalidated_reason": "operator_rejected", "metadata_json": support_meta},
            {"status": "pending", "metadata_json": support_meta},  # excluded
            {"status": "stale", "invalidated_reason": "superseded_by_new_draft", "metadata_json": support_meta},  # excluded
        ]
        stats = ev.aggregate_by_category(rows)

        sales = stats["Sales (415) 520-1316"]
        self.assertEqual((sales.accept, sales.reject, sales.excluded), (2, 1, 0))
        self.assertAlmostEqual(sales.accept_rate, 2 / 3)

        support = stats["Support (415) 999-0000"]
        self.assertEqual((support.accept, support.reject, support.excluded), (0, 1, 2))
        self.assertEqual(support.accept_rate, 0.0)

    def test_accept_rate_excludes_in_flight(self):
        # All pending -> no decided drafts -> accept_rate is None, not 0 or crash.
        rows = [{"status": "pending"}, {"status": "risk_pending"}]
        stats = ev.aggregate_by_category(rows)
        only = next(iter(stats.values()))
        self.assertIsNone(only.accept_rate)
        self.assertEqual(only.decided, 0)

    def test_category_falls_back_to_sender_then_unknown(self):
        rows = [
            {"status": "sent", "sender_number": "+14155201316", "metadata_json": None},
            {"status": "sent", "sender_number": None, "metadata_json": None},
        ]
        stats = ev.aggregate_by_category(rows)
        self.assertIn("+14155201316", stats)
        self.assertIn("unknown", stats)


class CrossDbJoinCastTests(unittest.TestCase):
    """Cross-DB join correctness: approval ``dialpad_sms_id`` (TEXT) -> messages.

    VERIFIED REALITY (probed against the live schema, not assumed): when the
    messages join column is declared ``INTEGER`` (as live), SQLite's numeric
    affinity rules coerce the TEXT id and a naive ``m.dialpad_id = d.dialpad_sms_id``
    join *does* match for in-range Dialpad ids. So the originally-claimed
    "naive join returns ZERO rows with INTEGER affinity" is NOT reproducible.

    The CAST is still the correct, chosen approach because it is
    affinity-INDEPENDENT: it returns the row whether the join column is declared
    INTEGER, NUMERIC, or has no type affinity at all. The
    ``test_no_affinity_join_*`` cases below prove a naive join genuinely returns
    zero rows when the column has no affinity -- which is the real trap the CAST
    guards against -- so the CAST in ``join_sent_message_text`` is load-bearing,
    not cosmetic.
    """

    def setUp(self):
        self.adb = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.mdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

        _make_approval_db(
            self.adb,
            [{"status": "sent", "dialpad_sms_id": "4628326630105088"}],
        )
        # messages.dialpad_id is INTEGER (matches live schema); approval id is TEXT.
        mconn = sqlite3.connect(self.mdb)
        mconn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, dialpad_id INTEGER UNIQUE, text TEXT)"
        )
        mconn.execute(
            "INSERT INTO messages (dialpad_id, text) VALUES (?, ?)",
            (4628326630105088, "the actual sent body"),
        )
        mconn.commit()
        mconn.close()

    def tearDown(self):
        for p in (self.adb, self.mdb):
            Path(p).unlink(missing_ok=True)

    def test_cast_join_returns_the_matching_row(self):
        # Primary correctness invariant: the eval resolves sent text for a real
        # id stored as TEXT against an INTEGER column.
        aconn = ev.connect_ro(self.adb)
        mconn = ev.connect_ro(self.mdb)
        try:
            result = ev.join_sent_message_text(
                aconn, mconn, dialpad_sms_ids=["4628326630105088"]
            )
        finally:
            aconn.close()
            mconn.close()
        self.assertEqual(result, {"4628326630105088": "the actual sent body"})

    def test_no_affinity_join_without_cast_returns_zero_rows(self):
        # The real trap: a join column with NO declared affinity storing an
        # integer does NOT coerce against a TEXT id -> naive join matches nothing.
        # This is exactly the regression the CAST prevents.
        mdb_noaff = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        try:
            mconn = sqlite3.connect(mdb_noaff)
            # Column declared WITHOUT a type name -> BLOB/no affinity.
            mconn.execute("CREATE TABLE messages (dialpad_id, text TEXT)")
            mconn.execute(
                "INSERT INTO messages (dialpad_id, text) VALUES (?, ?)",
                (4628326630105088, "the actual sent body"),
            )
            mconn.commit()
            mconn.close()

            aconn = ev.connect_ro(self.adb)
            try:
                aconn.execute(
                    "ATTACH DATABASE ? AS smsdb",
                    (f"file:{Path(mdb_noaff).as_posix()}?mode=ro",),
                )
                naive = aconn.execute(
                    "SELECT m.text FROM sms_approval_drafts d "
                    "JOIN smsdb.messages m ON m.dialpad_id = d.dialpad_sms_id"
                ).fetchall()
                cast = aconn.execute(
                    "SELECT m.text FROM sms_approval_drafts d "
                    "JOIN smsdb.messages m ON CAST(m.dialpad_id AS TEXT) = d.dialpad_sms_id"
                ).fetchall()
            finally:
                aconn.close()
        finally:
            Path(mdb_noaff).unlink(missing_ok=True)

        self.assertEqual(len(naive), 0)  # naive coercion fails for no-affinity columns
        self.assertEqual(len(cast), 1)   # the CAST makes it affinity-independent

    def test_cast_join_affinity_independent_for_no_affinity_column(self):
        # End-to-end: join_sent_message_text must still resolve the row when the
        # messages column has no affinity (proving the helper's CAST is the fix).
        mdb_noaff = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        try:
            mconn = sqlite3.connect(mdb_noaff)
            mconn.execute("CREATE TABLE messages (dialpad_id, text TEXT)")
            mconn.execute(
                "INSERT INTO messages (dialpad_id, text) VALUES (?, ?)",
                (4628326630105088, "the actual sent body"),
            )
            mconn.commit()
            mconn.close()

            aconn = ev.connect_ro(self.adb)
            mconn_ro = ev.connect_ro(mdb_noaff)
            try:
                result = ev.join_sent_message_text(
                    aconn, mconn_ro, dialpad_sms_ids=["4628326630105088"]
                )
            finally:
                aconn.close()
                mconn_ro.close()
        finally:
            Path(mdb_noaff).unlink(missing_ok=True)
        self.assertEqual(result, {"4628326630105088": "the actual sent body"})


class PiiSafetyTests(unittest.TestCase):
    """The pulse is human-facing; it must never leak customer SMS text/PII."""

    def test_pulse_output_contains_no_raw_message_text(self):
        secret_body = "CALL ME AT 415-867-5309 ABOUT MY ORDER"
        secret_phone = "+14158675309"
        secret_name = "Jane Q. Customer"
        meta = json.dumps({"line_display": "Sales (415) 520-1316"})
        rows = [
            {
                "status": "sent",
                "draft_text": secret_body,
                "customer_number": secret_phone,
                "metadata_json": meta,
            },
            {
                "status": "rejected",
                "invalidated_reason": "operator_rejected",
                "draft_text": secret_body,
                "customer_number": secret_phone,
                "metadata_json": meta,
            },
        ]
        # Need the actual draft_text/customer_number columns present in the row,
        # so go through a real DB read (not the trimmed-row helper).
        adb = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        try:
            _make_approval_db(adb, rows)
            conn = ev.connect_ro(adb)
            try:
                db_rows = ev.fetch_drafts(conn)
            finally:
                conn.close()
            pulse = ev.build_pulse(db_rows, start_ms=0, end_ms=2_000_000)
            blob = json.dumps(pulse) + "\n" + ev.render_pulse_text(pulse)
        finally:
            Path(adb).unlink(missing_ok=True)

        self.assertNotIn(secret_body, blob)
        self.assertNotIn(secret_phone, blob)
        self.assertNotIn(secret_name, blob)
        # The business-line category label IS allowed.
        self.assertIn("Sales (415) 520-1316", blob)

    def test_fetch_drafts_does_not_select_draft_text(self):
        # Belt-and-suspenders: the windowed query never pulls customer body.
        adb = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        try:
            _make_approval_db(adb, [{"status": "sent", "draft_text": "SECRET"}])
            conn = ev.connect_ro(adb)
            try:
                row = ev.fetch_drafts(conn)[0]
            finally:
                conn.close()
        finally:
            Path(adb).unlink(missing_ok=True)
        self.assertNotIn("draft_text", row.keys())


class DeterminismAndWindowTests(unittest.TestCase):
    def test_frozen_window_is_reproducible(self):
        # Two pulses over the same frozen [start,end] must be identical, and
        # a pending row inside the window must not change the decided counts.
        meta = json.dumps({"line_display": "Sales (415) 520-1316"})
        rows_data = [
            {"status": "sent", "created_at_ms": 1_500_000, "metadata_json": meta},
            {"status": "stale", "invalidated_reason": "manual_outbound", "created_at_ms": 1_600_000, "metadata_json": meta},
            {"status": "pending", "created_at_ms": 1_700_000, "metadata_json": meta},
            {"status": "sent", "created_at_ms": 9_999_999_999, "metadata_json": meta},  # outside window
        ]
        adb = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        try:
            _make_approval_db(adb, rows_data)
            conn = ev.connect_ro(adb)
            try:
                windowed = ev.fetch_drafts(conn, start_ms=1_000_000, end_ms=2_000_000)
            finally:
                conn.close()
            p1 = ev.build_pulse(windowed, start_ms=1_000_000, end_ms=2_000_000)
            p2 = ev.build_pulse(windowed, start_ms=1_000_000, end_ms=2_000_000)
        finally:
            Path(adb).unlink(missing_ok=True)

        self.assertEqual(p1, p2)  # reproducible
        self.assertEqual(p1["totals"]["drafts"], 3)  # the future row is excluded by window
        self.assertEqual(p1["totals"]["decided"], 2)  # pending excluded from decided
        self.assertEqual(p1["totals"]["accept"], 1)
        self.assertEqual(p1["totals"]["reject"], 1)

    def test_window_cutoff_math(self):
        start, end = ev.window_cutoff_ms(7, end_ms=1_000_000_000)
        self.assertEqual(end, 1_000_000_000)
        self.assertEqual(end - start, 7 * 24 * 60 * 60 * 1000)


class ReadOnlyTests(unittest.TestCase):
    def test_connection_rejects_writes(self):
        adb = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        try:
            _make_approval_db(adb, [{"status": "sent"}])
            conn = ev.connect_ro(adb)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("DELETE FROM sms_approval_drafts")
            finally:
                conn.close()
        finally:
            Path(adb).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
