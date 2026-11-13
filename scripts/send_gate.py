#!/usr/bin/env python3
"""Gated outbound SMS decision layer for shared team callers.

Every send through the wired entry points must pass four checks, failing
closed:

1. Caller/from-number allowlist from a JSON config
   (``DIALPAD_SEND_GATE_CONFIG``; a missing or unreadable config refuses).
2. Opt-out union: the Dialpad company opt-out list
   (``GET /api/v2/company/{id}/smsoptout?opt_out_state=opted_out`` with cursor
   pagination, company id from ``DIALPAD_COMPANY_ID``) UNION the local
   ``sms_approval_opt_outs`` table plus the emergency opt-out markers (read
   strictly). An unreadable or malformed source refuses.
3. Recipient-local send window 08:00 <= local time < 21:00, in EVERY timezone
   the recipient's NANP area code can cover (vendored table in
   ``nanp_area_code_timezones``; unknown area code or non-NANP refuses).
4. Recipient-local minute must not be 0/15/30/45.

Refusals for allowlist/opt-out/config are never retryable; window/minute
refusals are retryable and carry a ``retry_after_seconds`` hint.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import sms_approval
from nanp_area_code_timezones import timezones_for_e164

DIALPAD_API_BASE = "https://dialpad.com/api/v2"
CONFIG_ENV = "DIALPAD_SEND_GATE_CONFIG"
CALLER_ENV = "DIALPAD_SEND_CALLER"
COMPANY_ID_ENV = "DIALPAD_COMPANY_ID"
APPROVAL_DB_ENV = "DIALPAD_SMS_APPROVAL_DB"

WINDOW_START = time(8, 0)
WINDOW_END = time(21, 0)
BLOCKED_MINUTES = frozenset({0, 15, 30, 45})
_OPT_OUT_TIMEOUT_SECONDS = 15.0
_OPT_OUT_MAX_PAGES = 200
_RETRY_HORIZON_MINUTES = 26 * 60


class SendGateError(Exception):
    """Configuration or environment needed by the gate is missing or invalid."""


class OptOutSourceError(Exception):
    """An opt-out source cannot be read; the send must be refused."""


@dataclass
class Decision:
    """Outcome of a send-gate evaluation."""

    allowed: bool = False
    reasons: list[str] = field(default_factory=list)
    retryable: bool = False
    retry_after_seconds: int | None = None
    caller: str | None = None
    acting_for: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "retryable": self.retryable,
        }
        if self.retry_after_seconds is not None:
            data["retry_after_seconds"] = self.retry_after_seconds
        if self.caller:
            data["caller"] = self.caller
        if self.acting_for:
            data["acting_for"] = self.acting_for
        if self.details:
            data["checks"] = self.details
        return data

    def receipt_context(self) -> dict[str, Any]:
        """Caller attribution carried by the SMS receipt ledger."""
        context: dict[str, Any] = {}
        if self.caller:
            context["caller"] = self.caller
        if self.acting_for:
            context["acting_for"] = self.acting_for
        return context


@dataclass
class OptOutSources:
    """The two opt-out sources checked per recipient.

    ``company_opt_outs`` returns the normalized numbers on the Dialpad company
    opt-out list; ``local_opt_out`` answers for one number (any format). Both
    raise ``OptOutSourceError`` when unreadable.
    """

    company_opt_outs: Callable[[], set[str]]
    local_opt_out: Callable[[str], bool]


def _refuse(
    reasons: list[str],
    *,
    caller: str | None = None,
    acting_for: str | None = None,
    retryable: bool = False,
    retry_after_seconds: int | None = None,
    details: dict[str, Any] | None = None,
) -> Decision:
    return Decision(
        allowed=False,
        reasons=reasons,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
        caller=caller,
        acting_for=acting_for,
        details=details or {},
    )


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def _require_e164(number: Any, where: str) -> str:
    text = str(number or "").strip()
    if not text.startswith("+") or len(text) < 8 or not text[1:].isdigit():
        raise SendGateError(f"send gate config: {where} must be an E.164 number, got {number!r}")
    return text


def validate_config(config: Any) -> dict[str, Any]:
    """Fail closed on a malformed config: any invalid shape refuses all sends."""
    if not isinstance(config, dict):
        raise SendGateError("send gate config: top level must be a JSON object")
    shared_lines = config.get("shared_lines")
    teammates = config.get("teammates")
    callers = config.get("callers")
    for name, section in (("shared_lines", shared_lines), ("teammates", teammates), ("callers", callers)):
        if not isinstance(section, dict) or not section:
            raise SendGateError(f"send gate config: '{name}' must be a non-empty object")
    for label, mapping in (("shared_lines", shared_lines), ("teammates", teammates)):
        for key, number in mapping.items():
            _require_e164(number, f"{label}.{key}")
    for caller_id, entry in callers.items():
        if not isinstance(entry, dict):
            raise SendGateError(f"send gate config: callers.{caller_id} must be an object")
        lines = entry.get("shared_lines", [])
        if not isinstance(lines, list):
            raise SendGateError(f"send gate config: callers.{caller_id}.shared_lines must be a list")
        for line_name in lines:
            if not isinstance(line_name, str) or line_name not in shared_lines:
                raise SendGateError(
                    f"send gate config: callers.{caller_id}.shared_lines names unknown line {line_name!r}"
                )
        acts_for = entry.get("acts_for")
        if acts_for is not None and not isinstance(acts_for, str):
            raise SendGateError(f"send gate config: callers.{caller_id}.acts_for must be a teammate name")
        if acts_for is not None and acts_for not in teammates:
            raise SendGateError(
                f"send gate config: callers.{caller_id}.acts_for names unknown teammate {acts_for!r}"
            )
        extra = entry.get("extra_allowed_lines", [])
        if not isinstance(extra, list):
            raise SendGateError(f"send gate config: callers.{caller_id}.extra_allowed_lines must be a list")
        for number in extra:
            _require_e164(number, f"callers.{caller_id}.extra_allowed_lines")
        if "time_restricted" in entry and not isinstance(entry["time_restricted"], bool):
            raise SendGateError(f"send gate config: callers.{caller_id}.time_restricted must be a boolean")
    return config


def load_config(path: Path | str) -> dict[str, Any]:
    try:
        parsed = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SendGateError(f"send gate config unreadable at {path}: {exc}") from exc
    return validate_config(parsed)


def load_config_from_env(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    environ = os.environ if env is None else env
    raw = (environ.get(CONFIG_ENV) or "").strip()
    if not raw:
        raise SendGateError(f"{CONFIG_ENV} is not set")
    return load_config(raw)


def _canonical_sender(number: Any) -> str:
    """Strict sender form for the allowlist: exact match, except that a bare
    10-digit or 11-digit 1-prefixed NANP national form reads as +1....
    """
    text = str(number or "").strip()
    if text.isdigit():
        if len(text) == 10:
            return "+1" + text
        if len(text) == 11 and text.startswith("1"):
            return "+1" + text[1:]
    return text


def _allowed_lines(config: dict[str, Any], caller_entry: dict[str, Any]) -> set[str]:
    numbers: list[str] = []
    shared_lines = config.get("shared_lines") or {}
    for name in caller_entry.get("shared_lines") or []:
        numbers.append(shared_lines.get(name, ""))
    acts_for = caller_entry.get("acts_for")
    if acts_for:
        numbers.append((config.get("teammates") or {}).get(acts_for, ""))
    numbers.extend(caller_entry.get("extra_allowed_lines") or [])
    return {_canonical_sender(number) for number in numbers if number}


# --------------------------------------------------------------------------
# Opt-out sources
# --------------------------------------------------------------------------


def fetch_company_opt_outs(
    *,
    company_id: str,
    api_key: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = _OPT_OUT_TIMEOUT_SECONDS,
    base_url: str = DIALPAD_API_BASE,
) -> set[str]:
    """Fetch the Dialpad company opted_out list, following cursor pagination.

    Returns normalized phone numbers. Raises OptOutSourceError on HTTP errors,
    timeouts, or malformed bodies so the caller can fail closed.
    """
    opted_out: set[str] = set()
    cursor: str | None = None
    seen_cursors: set[str] = set()
    page = 0
    while True:
        page += 1
        if page > _OPT_OUT_MAX_PAGES:
            raise OptOutSourceError(
                f"dialpad opt-out list exceeded {_OPT_OUT_MAX_PAGES} pages"
            )
        query = {"opt_out_state": "opted_out"}
        if cursor:
            query["cursor"] = cursor
        url = f"{base_url}/company/{company_id}/smsoptout?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
            method="GET",
        )
        try:
            with opener(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise OptOutSourceError(f"dialpad opt-out list HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OptOutSourceError(f"dialpad opt-out list unreachable: {exc}") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise OptOutSourceError("dialpad opt-out list malformed body") from exc
        if not isinstance(body, dict) or "items" not in body:
            raise OptOutSourceError("dialpad opt-out list malformed body")
        items = body["items"]
        cursor = body.get("cursor") or None
        if items is None:
            if cursor is not None:
                raise OptOutSourceError("dialpad opt-out list malformed body")
            items = []
        if not isinstance(items, list):
            raise OptOutSourceError("dialpad opt-out list malformed body")
        for item in items:
            if (
                not isinstance(item, dict)
                or "opt_out_state" not in item
                or "external_endpoint" not in item
            ):
                raise OptOutSourceError("dialpad opt-out list malformed body")
            if item["opt_out_state"] != "opted_out":
                continue
            normalized = sms_approval.normalize_phone_number(item.get("external_endpoint"))
            if normalized:
                opted_out.add(normalized)
        if cursor is None:
            return opted_out
        if cursor in seen_cursors:
            raise OptOutSourceError("dialpad opt-out list repeated pagination cursor")
        seen_cursors.add(cursor)


def _approval_db_path(env: Mapping[str, str]) -> Path:
    raw = (env.get(APPROVAL_DB_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(sms_approval.DB_PATH).expanduser()


def env_opt_out_sources(env: Mapping[str, str] | None = None) -> OptOutSources:
    """Build opt-out sources from the environment, resolving lazily.

    Both sources resolve on first use so an allowlist refusal never spends a
    network call, and a resolved source is cached for the decision.
    """
    environ = os.environ if env is None else env
    company_cache: dict[str, set[str]] = {}
    conn_cache: dict[str, sqlite3.Connection] = {}

    def company_opt_outs() -> set[str]:
        if "set" in company_cache:
            return company_cache["set"]
        company_id = (environ.get(COMPANY_ID_ENV) or "").strip()
        if not company_id:
            raise OptOutSourceError(f"{COMPANY_ID_ENV} is not set")
        api_key = (environ.get("DIALPAD_API_KEY") or environ.get("DIALPAD_TOKEN") or "").strip()
        if not api_key:
            raise OptOutSourceError("DIALPAD_API_KEY is not set")
        resolved = fetch_company_opt_outs(company_id=company_id, api_key=api_key)
        company_cache["set"] = resolved
        return resolved

    def _approval_conn() -> sqlite3.Connection:
        if "conn" in conn_cache:
            return conn_cache["conn"]
        path = _approval_db_path(environ)
        if not path.is_file():
            raise OptOutSourceError(f"sms approval opt-out db missing: {path}")
        try:
            conn = sqlite3.connect(f"file:{path.resolve()}?mode=rw", uri=True)
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sms_approval_opt_outs'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise OptOutSourceError(f"sms approval opt-out db unreadable: {exc}") from exc
        if table is None:
            conn.close()
            raise OptOutSourceError("sms approval opt-out table missing: sms_approval_opt_outs")
        conn.row_factory = sqlite3.Row
        conn_cache["conn"] = conn
        return conn

    def _emergency_opted_out(normalized: str) -> bool:
        """Strict read of the emergency opt-out markers.

        Unlike sms_approval.is_emergency_opted_out (which other callers rely on
        staying best-effort), a marker file that exists but is unreadable or
        malformed raises OptOutSourceError so the send fails closed. A missing
        marker file is fine.
        """
        if normalized in sms_approval._EMERGENCY_OPT_OUT_MEMORY:
            return True
        for path in sms_approval.emergency_opt_out_paths():
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except (OSError, UnicodeError) as exc:
                raise OptOutSourceError(
                    f"emergency opt-out markers unreadable at {path}: {exc}"
                ) from exc
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise OptOutSourceError(
                        f"emergency opt-out marker malformed in {path}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise OptOutSourceError(f"emergency opt-out marker malformed in {path}")
                if payload.get("customer_number_normalized") == normalized:
                    return True
        return False

    def local_opt_out(number: str) -> bool:
        normalized = sms_approval.normalize_phone_number(number)
        if not normalized:
            return False
        if _emergency_opted_out(normalized):
            return True
        conn = _approval_conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM sms_approval_opt_outs WHERE customer_number_normalized = ?",
                (normalized,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise OptOutSourceError(f"sms approval opt-out lookup failed: {exc}") from exc
        return row is not None

    return OptOutSources(company_opt_outs=company_opt_outs, local_opt_out=local_opt_out)


# --------------------------------------------------------------------------
# Time checks
# --------------------------------------------------------------------------


def _window_ok(local_dt: datetime) -> bool:
    return WINDOW_START <= local_dt.time() < WINDOW_END


def _minute_ok(local_dt: datetime) -> bool:
    return local_dt.minute not in BLOCKED_MINUTES


def _next_pass_seconds(now_utc: datetime, zones: list[ZoneInfo]) -> int | None:
    """Seconds until every zone is inside the window at an allowed minute."""
    candidate = now_utc.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(_RETRY_HORIZON_MINUTES):
        if all(
            _window_ok(candidate.astimezone(zone)) and _minute_ok(candidate.astimezone(zone))
            for zone in zones
        ):
            return int((candidate - now_utc).total_seconds())
        candidate += timedelta(minutes=1)
    return None


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def evaluate_send(
    *,
    caller: str | None,
    from_number: str | None,
    to_numbers: list[str],
    now_utc: datetime,
    opt_out_sources: OptOutSources,
    config: dict[str, Any],
) -> Decision:
    """Pure gate evaluation: all checks must pass; the send fails closed."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    caller_id = (caller or "").strip() or None
    callers = config.get("callers") if isinstance(config, dict) else None
    entry = (callers or {}).get(caller_id or "", None)
    if not isinstance(entry, dict):
        return _refuse(
            [f"send gate: unknown caller {caller_id or '<unset>'!r}"],
            caller=caller_id,
        )
    acting_for = entry.get("acts_for")

    sender = _canonical_sender(from_number)
    allowed_lines = _allowed_lines(config, entry)
    if sender not in allowed_lines:
        return _refuse(
            [
                f"send gate: from number {from_number or '<unset>'!r} is not an approved "
                f"line for caller {caller_id}"
            ],
            caller=caller_id,
            acting_for=acting_for,
        )

    try:
        company_opted_out = opt_out_sources.company_opt_outs()
    except OptOutSourceError as exc:
        return _refuse(
            [f"send gate: opt-out source unavailable: {exc}"],
            caller=caller_id,
            acting_for=acting_for,
        )
    except Exception as exc:  # noqa: BLE001 - an unplumbed opt-out failure fails closed too.
        return _refuse(
            [f"send gate: opt-out source unavailable: {exc}"],
            caller=caller_id,
            acting_for=acting_for,
        )

    time_restricted = entry.get("time_restricted", True) is not False
    hard_reasons: list[str] = []
    time_reasons: list[str] = []
    details: dict[str, Any] = {"caller": caller_id, "acting_for": acting_for, "recipients": {}}
    all_zones: list[ZoneInfo] = []

    for number in to_numbers:
        normalized_to = sms_approval.normalize_phone_number(number)
        recipient: dict[str, Any] = {}
        details["recipients"][str(number)] = recipient

        if normalized_to and normalized_to in company_opted_out:
            hard_reasons.append(f"recipient {number} is opted out (dialpad company list)")
            recipient["opted_out"] = "dialpad_company"
            continue
        try:
            if opt_out_sources.local_opt_out(number):
                hard_reasons.append(f"recipient {number} is opted out (local opt-out list)")
                recipient["opted_out"] = "local"
                continue
        except OptOutSourceError as exc:
            return _refuse(
                [f"send gate: opt-out source unavailable: {exc}"],
                caller=caller_id,
                acting_for=acting_for,
                details=details,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed on any opt-out lookup error.
            return _refuse(
                [f"send gate: opt-out source unavailable: {exc}"],
                caller=caller_id,
                acting_for=acting_for,
                details=details,
            )
        recipient["opted_out"] = False

        zones = list(timezones_for_e164(number))
        if not zones:
            hard_reasons.append(f"recipient {number} has an unknown or non-US/Canada NANP area code")
            continue
        recipient["timezones"] = [zone.key for zone in zones]
        all_zones.extend(zones)

        if not time_restricted:
            continue
        for zone in zones:
            local_dt = now_utc.astimezone(zone)
            recipient.setdefault("local_time", {})[zone.key] = local_dt.isoformat()
            if not _window_ok(local_dt):
                time_reasons.append(
                    f"recipient {number} outside send window: {local_dt.strftime('%H:%M')} local ({zone.key})"
                )
            elif not _minute_ok(local_dt):
                time_reasons.append(
                    f"recipient {number} blocked at minute :{local_dt.minute:02d} local ({zone.key})"
                )

    reasons = hard_reasons + time_reasons
    if not reasons:
        return Decision(
            allowed=True,
            reasons=[],
            retryable=False,
            caller=caller_id,
            acting_for=acting_for,
            details=details,
        )

    retryable = bool(time_reasons) and not hard_reasons
    retry_after = _next_pass_seconds(now_utc, all_zones) if retryable else None
    return _refuse(
        reasons,
        caller=caller_id,
        acting_for=acting_for,
        retryable=retryable,
        retry_after_seconds=retry_after,
        details=details,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def evaluate_from_environment(
    *,
    caller: str | None,
    from_number: str | None,
    to_numbers: list[str],
    now_utc: datetime | None = None,
    opt_out_sources: OptOutSources | None = None,
    env: Mapping[str, str] | None = None,
) -> Decision:
    """Config + opt-out sources from the environment, then the pure evaluation."""
    environ = os.environ if env is None else env
    try:
        config = load_config_from_env(environ)
    except SendGateError as exc:
        return _refuse([f"send gate: {exc}"], caller=(caller or "").strip() or None)
    except Exception as exc:  # noqa: BLE001 - any config failure fails closed.
        return _refuse(
            [f"send gate: config unavailable: {exc}"],
            caller=(caller or "").strip() or None,
        )
    sources = opt_out_sources or env_opt_out_sources(environ)
    return evaluate_send(
        caller=caller,
        from_number=from_number,
        to_numbers=to_numbers,
        now_utc=now_utc or _utc_now(),
        opt_out_sources=sources,
        config=config,
    )
