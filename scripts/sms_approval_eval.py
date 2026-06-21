#!/usr/bin/env python3
"""Read-only evaluation of the Dialpad SMS approval gate.

Computes per-category accept/reject statistics and a human-facing weekly
pulse from the approval database (and, optionally, the SMS message store for
cross-DB joins). This module never writes: every connection is opened in
SQLite read-only mode (``file:...?mode=ro``).

Maintainer decisions encoded here (do not "fix" without re-deciding):

* An operator hand-typing their own reply instead of approving the draft
  (``invalidated_reason == 'manual_outbound'``) is a draft REJECTION, not an
  accept. The draft was not good enough to send as-is.
* In-flight ``pending`` / ``risk_pending`` drafts are EXCLUDED from
  accept/reject rates: their outcome is not yet determined, so including them
  makes the rate non-deterministic run-to-run.
* ``stale`` drafts are split by ``invalidated_reason`` rather than bucketed as
  one "abandoned" group. ``manual_outbound`` is a reject;
  ``superseded_by_new_draft`` is EXCLUDED (the customer texted again before the
  operator acted — not a verdict on the draft); other reasons map explicitly.
* The cross-DB join from approval ``dialpad_sms_id`` (TEXT) to messages
  ``dialpad_id`` uses an explicit ``CAST(... AS TEXT)``. With the live INTEGER
  column SQLite's numeric affinity happens to coerce, but a no-affinity column
  (or an id beyond INT64) silently joins zero rows; the CAST makes the
  comparison affinity-independent. See ``join_sent_message_text``.
* The pulse is aggregate-only: it must never embed customer SMS bodies, phone
  numbers, or contact names.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# --- status taxonomy (mirrors scripts/sms_approval.py constants) -------------

STATUS_PENDING = "pending"
STATUS_RISK_PENDING = "risk_pending"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_STALE = "stale"
STATUS_REJECTED = "rejected"

# Every status a draft row can hold. The partition over these must be
# exhaustive: see assert_exhaustive_partition.
ALL_STATUSES: tuple[str, ...] = (
    STATUS_PENDING,
    STATUS_RISK_PENDING,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_FAILED,
    STATUS_STALE,
    STATUS_REJECTED,
)

# Statuses whose outcome is not yet determined; excluded from accept/reject
# rates so the pulse is reproducible run-to-run for a frozen window.
IN_FLIGHT_STATUSES: frozenset[str] = frozenset({STATUS_PENDING, STATUS_RISK_PENDING, STATUS_SENDING})

# Verdict buckets a draft is classified into.
VERDICT_ACCEPT = "accept"
VERDICT_REJECT = "reject"
VERDICT_EXCLUDED = "excluded"  # in-flight or non-verdict (e.g. superseded)

# invalidated_reason -> verdict, for rows in STATUS_STALE.
#   manual_outbound       : operator hand-typed a reply -> draft REJECTED.
#   superseded_by_new_draft: customer texted again first -> not a verdict.
#   operator_rejected     : explicit reject (also surfaces as STATUS_REJECTED).
#   new_inbound_not_eligible: inbound made the draft moot -> not a verdict.
#   smoke_test_cleanup    : test fixture teardown -> not a verdict.
STALE_REASON_VERDICTS: dict[str, str] = {
    "manual_outbound": VERDICT_REJECT,
    "operator_rejected": VERDICT_REJECT,
    "superseded_by_new_draft": VERDICT_EXCLUDED,
    "new_inbound_not_eligible": VERDICT_EXCLUDED,
    "smoke_test_cleanup": VERDICT_EXCLUDED,
}
# A stale row with an unrecognized / missing reason is excluded rather than
# silently counted as accept or reject.
DEFAULT_STALE_VERDICT = VERDICT_EXCLUDED

DEFAULT_APPROVAL_DB = "/home/art/clawd/logs/sms_approvals.db"
DEFAULT_SMS_DB = "/home/art/clawd/logs/sms.db"
DEFAULT_WINDOW_DAYS = 7


# --- read-only connections ---------------------------------------------------


def connect_ro(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite database in read-only mode.

    Uses the URI ``mode=ro`` form so the eval can never mutate live data, and
    sets ``PRAGMA query_only`` as belt-and-suspenders.
    """
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def resolve_approval_db() -> str:
    return os.environ.get("DIALPAD_SMS_APPROVAL_DB", DEFAULT_APPROVAL_DB)


def resolve_sms_db() -> str:
    return os.environ.get("DIALPAD_SMS_DB", DEFAULT_SMS_DB)


# --- verdict classification --------------------------------------------------


def classify_verdict(status: str, invalidated_reason: str | None) -> str:
    """Map (status, invalidated_reason) -> accept / reject / excluded.

    * sent / failed are terminal send outcomes: sent == accept, failed == an
      accepted-but-undelivered draft (the operator approved it; delivery is a
      separate axis), so failed counts as accept for the *approval* verdict.
    * rejected == reject.
    * stale is split by invalidated_reason via STALE_REASON_VERDICTS.
    * in-flight (pending / risk_pending / sending) == excluded.
    """
    if status in IN_FLIGHT_STATUSES:
        return VERDICT_EXCLUDED
    if status in (STATUS_SENT, STATUS_FAILED):
        return VERDICT_ACCEPT
    if status == STATUS_REJECTED:
        return VERDICT_REJECT
    if status == STATUS_STALE:
        reason = (invalidated_reason or "").strip()
        return STALE_REASON_VERDICTS.get(reason, DEFAULT_STALE_VERDICT)
    # Unknown status: never silently bucket as a verdict.
    return VERDICT_EXCLUDED


@dataclass
class CategoryStats:
    category: str
    accept: int = 0
    reject: int = 0
    excluded: int = 0
    # status -> count, for the exhaustiveness check and drill-down.
    by_status: dict[str, int] = field(default_factory=dict)

    @property
    def decided(self) -> int:
        return self.accept + self.reject

    @property
    def total(self) -> int:
        return self.accept + self.reject + self.excluded

    @property
    def accept_rate(self) -> float | None:
        """Accept rate over DECIDED drafts only (excludes in-flight/superseded).

        None when there are no decided drafts, so callers can render "n/a"
        rather than dividing by zero.
        """
        if self.decided == 0:
            return None
        return self.accept / self.decided

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "accept": self.accept,
            "reject": self.reject,
            "excluded": self.excluded,
            "decided": self.decided,
            "total": self.total,
            "accept_rate": self.accept_rate,
            "by_status": dict(sorted(self.by_status.items())),
        }


def category_for_row(row: sqlite3.Row | dict[str, Any]) -> str:
    """Derive the per-category label for a draft row.

    Primary dimension is the Dialpad line (``metadata.line_display``, e.g.
    "Sales (415) 520-1316"). Falls back to the sender number, then to
    "unknown". The label is a business line, never customer PII.
    """
    meta_raw = row["metadata_json"] if "metadata_json" in row.keys() else None
    if meta_raw:
        try:
            meta = json.loads(meta_raw)
        except (json.JSONDecodeError, TypeError):
            meta = {}
        line = meta.get("line_display")
        if isinstance(line, str) and line.strip():
            return line.strip()
    sender = row["sender_number"] if "sender_number" in row.keys() else None
    if isinstance(sender, str) and sender.strip():
        return sender.strip()
    return "unknown"


# --- window selection --------------------------------------------------------


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def window_cutoff_ms(window_days: int, *, end_ms: int | None = None) -> tuple[int, int]:
    """Return (start_ms, end_ms) for a frozen window.

    end_ms defaults to now; pass an explicit end_ms for a reproducible window.
    """
    end = end_ms if end_ms is not None else now_ms()
    start = end - window_days * 24 * 60 * 60 * 1000
    return start, end


def fetch_drafts(
    conn: sqlite3.Connection,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[sqlite3.Row]:
    """Fetch draft rows, optionally windowed by created_at_ms [start, end]."""
    clauses: list[str] = []
    params: list[Any] = []
    if start_ms is not None:
        clauses.append("created_at_ms >= ?")
        params.append(start_ms)
    if end_ms is not None:
        clauses.append("created_at_ms <= ?")
        params.append(end_ms)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT draft_id, status, invalidated_reason, sender_number, "
        "metadata_json, created_at_ms, dialpad_sms_id "
        f"FROM sms_approval_drafts{where}"
    )
    return list(conn.execute(sql, params).fetchall())


# --- aggregation -------------------------------------------------------------


def assert_exhaustive_partition(rows: Iterable[sqlite3.Row | dict[str, Any]]) -> dict[str, int]:
    """Verify every row's status is one of ALL_STATUSES.

    Returns the status histogram. Raises ValueError if any row carries a status
    outside the known taxonomy, so a new status added upstream is caught loudly
    instead of being silently dropped from the buckets.
    """
    histogram: dict[str, int] = {}
    unknown: dict[str, int] = {}
    for row in rows:
        status = row["status"]
        histogram[status] = histogram.get(status, 0) + 1
        if status not in ALL_STATUSES:
            unknown[status] = unknown.get(status, 0) + 1
    if unknown:
        raise ValueError(f"unknown approval statuses outside taxonomy: {sorted(unknown)}")
    return histogram


def aggregate_by_category(rows: Iterable[sqlite3.Row | dict[str, Any]]) -> dict[str, CategoryStats]:
    """Aggregate verdicts per category. Asserts the status partition is total."""
    rows = list(rows)
    histogram = assert_exhaustive_partition(rows)
    # Cross-check: sum of bucket counts == total rows (no row silently dropped).
    assert sum(histogram.values()) == len(rows), "status histogram lost rows"

    stats: dict[str, CategoryStats] = {}
    for row in rows:
        category = category_for_row(row)
        status = row["status"]
        reason = row["invalidated_reason"] if "invalidated_reason" in row.keys() else None
        verdict = classify_verdict(status, reason)

        bucket = stats.setdefault(category, CategoryStats(category=category))
        bucket.by_status[status] = bucket.by_status.get(status, 0) + 1
        if verdict == VERDICT_ACCEPT:
            bucket.accept += 1
        elif verdict == VERDICT_REJECT:
            bucket.reject += 1
        else:
            bucket.excluded += 1

    # Final invariant: per category, accept+reject+excluded == rows in category.
    for bucket in stats.values():
        assert bucket.total == sum(bucket.by_status.values()), (
            f"category {bucket.category} verdict counts diverge from status counts"
        )
    return stats


# --- cross-DB join (the CAST bug) -------------------------------------------


def join_sent_message_text(
    approval_conn: sqlite3.Connection,
    sms_conn: sqlite3.Connection,
    *,
    dialpad_sms_ids: Iterable[str],
) -> dict[str, str]:
    """Resolve sent message text by joining approval ids to the SMS store.

    The approval DB stores ``dialpad_sms_id`` as TEXT; the SMS store stores
    ``messages.dialpad_id`` as INTEGER. Comparing them in a join predicate is an
    affinity hazard: with the live INTEGER column, SQLite's numeric affinity
    rules happen to coerce in-range Dialpad ids so a naive
    ``messages.dialpad_id = drafts.dialpad_sms_id`` does match -- but that
    coercion is NOT guaranteed: a join column with no affinity (or an id beyond
    INT64) silently matches ZERO rows. The explicit
    ``CAST(messages.dialpad_id AS TEXT)`` makes the comparison TEXT-to-TEXT and
    affinity-independent, so this resolves correctly regardless of how the
    messages column is declared. (See test_sms_approval_eval CrossDbJoinCastTests.)

    This helper exists for ad-hoc operator drill-down and to anchor the
    regression test; the pulse itself never embeds the returned text.
    """
    ids = [i for i in dialpad_sms_ids if i]
    if not ids:
        return {}
    # Both connections are read-only; attach the SMS store to run a single join.
    sms_path = sms_conn.execute("PRAGMA database_list").fetchone()["file"]
    approval_conn.execute("ATTACH DATABASE ? AS smsdb", (f"file:{sms_path}?mode=ro",))
    try:
        placeholders = ",".join("?" for _ in ids)
        sql = (
            "SELECT d.dialpad_sms_id AS sms_id, m.text AS text "
            "FROM sms_approval_drafts d "
            "JOIN smsdb.messages m "
            "  ON CAST(m.dialpad_id AS TEXT) = d.dialpad_sms_id "
            f"WHERE d.dialpad_sms_id IN ({placeholders})"
        )
        return {row["sms_id"]: row["text"] for row in approval_conn.execute(sql, ids).fetchall()}
    finally:
        approval_conn.execute("DETACH DATABASE smsdb")


# --- weekly pulse (aggregate-only, PII-free) --------------------------------


def build_pulse(
    rows: Iterable[sqlite3.Row | dict[str, Any]],
    *,
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    """Build the aggregate-only weekly pulse.

    The output contains ONLY counts, rates, category labels (business lines),
    and timestamps. It never contains customer SMS bodies, phone numbers, or
    contact names.
    """
    rows = list(rows)
    by_category = aggregate_by_category(rows)
    status_histogram = assert_exhaustive_partition(rows)

    total_accept = sum(c.accept for c in by_category.values())
    total_reject = sum(c.reject for c in by_category.values())
    total_excluded = sum(c.excluded for c in by_category.values())
    total_decided = total_accept + total_reject

    categories = [c.to_dict() for c in sorted(by_category.values(), key=lambda c: (-c.decided, c.category))]

    return {
        "window": {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start_iso": datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(),
            "end_iso": datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).isoformat(),
        },
        "totals": {
            "drafts": len(rows),
            "accept": total_accept,
            "reject": total_reject,
            "excluded": total_excluded,
            "decided": total_decided,
            "accept_rate": (total_accept / total_decided) if total_decided else None,
        },
        "status_histogram": dict(sorted(status_histogram.items())),
        "categories": categories,
    }


def render_pulse_text(pulse: dict[str, Any]) -> str:
    """Render the pulse as a compact human-readable report (aggregate-only)."""
    w = pulse["window"]
    t = pulse["totals"]
    lines: list[str] = []
    lines.append(f"SMS approval pulse  {w['start_iso'][:10]} -> {w['end_iso'][:10]}")
    rate = t["accept_rate"]
    rate_str = f"{rate * 100:.0f}%" if rate is not None else "n/a"
    lines.append(
        f"  drafts={t['drafts']}  decided={t['decided']}  "
        f"accept={t['accept']} reject={t['reject']} (excluded={t['excluded']})  "
        f"accept_rate={rate_str}"
    )
    lines.append("  by status: " + ", ".join(f"{k}={v}" for k, v in pulse["status_histogram"].items()))
    lines.append("  by category (decided / accept_rate):")
    for cat in pulse["categories"]:
        cr = cat["accept_rate"]
        cr_str = f"{cr * 100:.0f}%" if cr is not None else "n/a"
        lines.append(
            f"    - {cat['category']}: decided={cat['decided']} "
            f"accept={cat['accept']} reject={cat['reject']} rate={cr_str}"
        )
    return "\n".join(lines)


# --- CLI ---------------------------------------------------------------------


def run_pulse(args: argparse.Namespace) -> dict[str, Any]:
    approval_db = args.approval_db or resolve_approval_db()
    conn = connect_ro(approval_db)
    try:
        if args.end_ms is not None:
            end_ms = args.end_ms
        else:
            end_ms = now_ms()
        start_ms, end_ms = window_cutoff_ms(args.window_days, end_ms=end_ms)
        rows = fetch_drafts(conn, start_ms=start_ms, end_ms=end_ms)
        return build_pulse(rows, start_ms=start_ms, end_ms=end_ms)
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only SMS approval eval and weekly pulse")
    sub = parser.add_subparsers(dest="command", required=True)

    pulse = sub.add_parser("pulse", help="Weekly accept/reject pulse (aggregate-only)")
    pulse.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    pulse.add_argument(
        "--end-ms",
        type=int,
        default=None,
        help="Explicit window end (ms epoch) for a reproducible run; default now.",
    )
    pulse.add_argument("--approval-db", default=None, help="Override approval DB path.")
    pulse.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "pulse":
        pulse = run_pulse(args)
        if args.json:
            print(json.dumps(pulse, sort_keys=True))
        else:
            print(render_pulse_text(pulse))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
