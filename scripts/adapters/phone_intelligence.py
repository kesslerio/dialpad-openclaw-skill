#!/usr/bin/env python3
"""IPQS phone-intelligence adapter for Dialpad inbound enrichment.

The adapter is import-safe and CLI-safe. It normalizes a compact, allowlisted
subset of IPQualityScore phone validation data, caches only that sanitized shape,
and fails closed with a statused JSON object on every expected miss or provider
failure.
"""

import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


IPQS_ENDPOINT = os.environ.get(
    "IPQS_PHONE_VALIDATION_ENDPOINT",
    "https://www.ipqualityscore.com/api/json/phone",
).rstrip("/")
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_PUBLIC_SEARCH_TTL_SECONDS = 6 * 60 * 60
DEFAULT_BUDGET_WINDOW_SECONDS = 60 * 60
RISK_POLICY_VERSION = "risk-v1"
SAFE_STATUSES = {
    "usable",
    "not_configured",
    "not_found",
    "invalid",
    "inactive",
    "disposable",
    "risky",
    "unavailable",
    "timeout",
    "budget_exceeded",
    "rate_limited",
    "unsafe_output",
}
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clean(value, limit=160):
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = CONTROL_RE.sub(" ", str(value))
    text = " ".join(text.split())
    if not text:
        return None
    return text[:limit]


def _bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "active"}:
        return True
    if text in {"false", "0", "no", "inactive"}:
        return False
    return None


def normalize_phone(phone):
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if str(phone or "").strip().startswith("+") and 8 <= len(digits) <= 15:
        return f"+{digits}"
    return None


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def cache_db_path():
    raw = os.environ.get("DIALPAD_PHONE_INTELLIGENCE_CACHE_DB", "")
    if raw:
        return Path(raw).expanduser()
    state_dir = os.environ.get("DIALPAD_STATE_DIR") or os.environ.get("XDG_STATE_HOME")
    if state_dir:
        return Path(state_dir).expanduser() / "dialpad" / "phone_intelligence.db"
    return None


def _private_connect(path):
    path = Path(path)
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        path.parent.chmod(0o700)
    old_umask = os.umask(0o177)
    try:
        conn = sqlite3.connect(path)
        path.chmod(0o600)
    finally:
        os.umask(old_umask)
    conn.execute("PRAGMA busy_timeout=2500")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    _harden_sidecars(path)
    return conn


def _harden_sidecars(path):
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            sidecar.chmod(0o600)


def _init_cache(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS phone_intelligence_cache (
          phone TEXT NOT NULL,
          policy_version TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          expires_at INTEGER NOT NULL,
          payload TEXT NOT NULL,
          PRIMARY KEY (phone, policy_version)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS phone_intelligence_budget (
          kind TEXT NOT NULL,
          window_start INTEGER NOT NULL,
          token TEXT NOT NULL,
          PRIMARY KEY (kind, window_start, token)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS phone_intelligence_json_cache (
          namespace TEXT NOT NULL,
          cache_key TEXT NOT NULL,
          policy_version TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          expires_at INTEGER NOT NULL,
          payload TEXT NOT NULL,
          PRIMARY KEY (namespace, cache_key, policy_version)
        )
        """
    )
    conn.commit()


def lookup_policy_version(country_hint=None, strictness=None, enhanced_line_check=None):
    strict = str(strictness if strictness is not None else os.environ.get("IPQS_PHONE_STRICTNESS", "0"))
    country = str(country_hint or os.environ.get("IPQS_PHONE_COUNTRY_HINT", "US")).upper()
    enhanced = "1" if _bool(enhanced_line_check if enhanced_line_check is not None else os.environ.get("IPQS_ENHANCED_LINE_CHECK")) else "0"
    return "|".join((IPQS_ENDPOINT, f"strict={strict}", f"country={country}", f"enhanced={enhanced}", RISK_POLICY_VERSION))


def _cache_payload_get(select_sql, params, policy_version, now=None):
    path = cache_db_path()
    if not path:
        return None
    now = int(now or time.time())
    try:
        with _private_connect(path) as conn:
            _init_cache(conn)
            row = conn.execute(select_sql, (*params, now)).fetchone()
            conn.commit()
            if not row:
                return None
            payload = json.loads(row[0])
            if isinstance(payload, dict):
                payload["cache"] = {"hit": True, "policyVersion": policy_version}
                return payload
    except Exception:
        return None
    return None


def _cache_payload_set(delete_sql, insert_sql, params, payload, ttl_seconds, now=None):
    path = cache_db_path()
    if not path:
        return
    now = int(now or time.time())
    ttl = int(ttl_seconds)
    if ttl <= 0:
        return
    stored = dict(payload)
    stored.pop("cache", None)
    try:
        with _private_connect(path) as conn:
            _init_cache(conn)
            conn.execute(delete_sql, (now,))
            conn.execute(insert_sql, (*params, now, now + ttl, json.dumps(stored, separators=(",", ":"))))
            conn.commit()
            _harden_sidecars(path)
    except Exception:
        return


def _cache_get(phone, policy_version, now=None):
    return _cache_payload_get(
        """
        SELECT payload FROM phone_intelligence_cache
        WHERE phone = ? AND policy_version = ? AND expires_at > ?
        """,
        (phone, policy_version),
        policy_version,
        now=now,
    )


def _cache_set(phone, policy_version, payload, ttl_seconds=None, now=None):
    ttl = int(ttl_seconds if ttl_seconds is not None else _env_int("DIALPAD_PHONE_INTELLIGENCE_CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL_SECONDS))
    _cache_payload_set(
        "DELETE FROM phone_intelligence_cache WHERE expires_at <= ?",
        """
        INSERT OR REPLACE INTO phone_intelligence_cache
        (phone, policy_version, created_at, expires_at, payload)
        VALUES (?, ?, ?, ?, ?)
        """,
        (phone, policy_version),
        payload,
        ttl,
        now=now,
    )


def cache_json_get(namespace, cache_key, policy_version, now=None):
    return _cache_payload_get(
        """
        SELECT payload FROM phone_intelligence_json_cache
        WHERE namespace = ? AND cache_key = ? AND policy_version = ? AND expires_at > ?
        """,
        (namespace, cache_key, policy_version),
        policy_version,
        now=now,
    )


def cache_json_set(namespace, cache_key, policy_version, payload, ttl_seconds=None, now=None):
    ttl = int(ttl_seconds if ttl_seconds is not None else DEFAULT_PUBLIC_SEARCH_TTL_SECONDS)
    _cache_payload_set(
        "DELETE FROM phone_intelligence_json_cache WHERE expires_at <= ?",
        """
        INSERT OR REPLACE INTO phone_intelligence_json_cache
        (namespace, cache_key, policy_version, created_at, expires_at, payload)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (namespace, cache_key, policy_version),
        payload,
        ttl,
        now=now,
    )


def budget_available(kind, token, limit, window_seconds=None, now=None):
    if limit is None or int(limit) <= 0:
        return True
    path = cache_db_path()
    if not path:
        return False
    now = int(now or time.time())
    window = max(1, int(window_seconds or _env_int("DIALPAD_CALLER_INTELLIGENCE_BUDGET_WINDOW_SECONDS", DEFAULT_BUDGET_WINDOW_SECONDS)))
    window_start = now - (now % window)
    try:
        with _private_connect(path) as conn:
            _init_cache(conn)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM phone_intelligence_budget WHERE window_start < ?", (window_start,))
            count = conn.execute(
                "SELECT COUNT(*) FROM phone_intelligence_budget WHERE kind = ? AND window_start = ?",
                (kind, window_start),
            ).fetchone()[0]
            existing = conn.execute(
                "SELECT 1 FROM phone_intelligence_budget WHERE kind = ? AND window_start = ? AND token = ?",
                (kind, window_start, token),
            ).fetchone()
            if not existing and count >= int(limit):
                conn.commit()
                return False
            conn.execute(
                "INSERT OR IGNORE INTO phone_intelligence_budget (kind, window_start, token) VALUES (?, ?, ?)",
                (kind, window_start, token),
            )
            conn.commit()
            _harden_sidecars(path)
            return True
    except Exception:
        return False


def _risk_from_payload(payload):
    valid = _bool(payload.get("valid"))
    active = _bool(payload.get("active"))
    active_status = _clean(payload.get("active_status") or payload.get("line_status"), limit=40)
    inactive_status_terms = (
        "inactive",
        "disconnected",
        "turned off",
        "not in service",
        "unreachable",
    )
    if active is False or (
        active_status
        and any(term in active_status.lower() for term in inactive_status_terms)
    ):
        active_status = "inactive"
    elif active is True:
        active_status = "active"
    else:
        active_status = "unknown"

    line_type = (_clean(payload.get("line_type") or payload.get("type"), limit=40) or "").lower()
    temporary = any(term in line_type for term in ("disposable", "temporary"))
    recent_abuse = bool(_bool(payload.get("recent_abuse")))
    risky = bool(_bool(payload.get("risky")))
    spammer = bool(_bool(payload.get("spammer")) or _bool(payload.get("active_spammer")))
    try:
        fraud_score = int(float(payload.get("fraud_score") or 0))
    except (TypeError, ValueError):
        fraud_score = 0

    reasons = []
    if valid is False:
        reasons.append("invalid")
    if active_status == "inactive":
        reasons.append("inactive")
    if temporary:
        reasons.append("disposable")
    if recent_abuse:
        reasons.append("recent_abuse")
    if risky:
        reasons.append("risky")
    if spammer:
        reasons.append("spammer")
    if fraud_score >= 85:
        reasons.append("fraud_score")

    if reasons:
        return "high", reasons, active_status, fraud_score, temporary
    if fraud_score >= 75:
        return "medium", ["fraud_score"], active_status, fraud_score, temporary
    return "low", [], active_status, fraud_score, temporary


def normalize_ipqs_payload(phone, payload):
    if not isinstance(payload, dict):
        return degraded("unavailable", phone=phone)
    if payload.get("success") is False and _bool(payload.get("valid")) is not False:
        message = str(payload.get("message") or payload.get("error") or "").lower()
        if any(term in message for term in ("rate", "quota", "limit", "too many")):
            return degraded("rate_limited", phone=phone)
        return degraded("unavailable", phone=phone)

    normalized = normalize_phone(payload.get("formatted") or payload.get("phone") or phone) or normalize_phone(phone)
    risk_level, reasons, active_status, fraud_score, temporary = _risk_from_payload(payload)
    reasons = list(reasons)
    valid = _bool(payload.get("valid"))
    status = "usable"
    usable = True
    if valid is not True:
        status = "invalid"
        usable = False
        risk_level = "high"
        if "invalid" not in reasons:
            reasons.append("invalid")
    elif active_status == "inactive":
        status = "inactive"
        usable = False
    elif temporary:
        status = "disposable"
        usable = False
    elif risk_level == "high":
        status = "risky"
        usable = False

    return {
        "usable": usable,
        "status": status,
        "source": "ipqs",
        "phone": {
            "e164": normalized,
            "localFormat": _clean(payload.get("formatted") or payload.get("local_format"), limit=40),
            "country": _clean(payload.get("country") or payload.get("country_code"), limit=40),
            "region": _clean(payload.get("region") or payload.get("state"), limit=80),
            "city": _clean(payload.get("city"), limit=80),
            "timezone": _clean(payload.get("timezone"), limit=80),
        },
        "line": {
            "carrier": _clean(payload.get("carrier"), limit=120),
            "type": _clean(payload.get("line_type") or payload.get("type"), limit=40),
            "active": None if active_status == "unknown" else active_status == "active",
            "activeStatus": active_status,
        },
        "risk": {
            "level": risk_level,
            "fraudScore": fraud_score,
            "recentAbuse": bool(_bool(payload.get("recent_abuse"))),
            "risky": bool(_bool(payload.get("risky"))),
            "spammer": bool(_bool(payload.get("spammer")) or _bool(payload.get("active_spammer"))),
            "reasons": reasons,
        },
        "possibleIdentity": {
            "reverseName": _clean(payload.get("name") or payload.get("owner") or payload.get("caller_name"), limit=120),
            "basis": "ipqs_reverse_lookup",
            "confidence": "low",
        },
    }


def degraded(status, *, phone=None):
    status = status if status in SAFE_STATUSES else "unavailable"
    return {
        "usable": False,
        "status": status,
        "source": "ipqs",
        "phone": {"e164": normalize_phone(phone)},
        "risk": {"level": "unknown", "reasons": []},
    }


def _ipqs_request(phone, api_key, timeout, country_hint, strictness, enhanced_line_check):
    params = {
        "strictness": str(strictness),
        "country": str(country_hint or "US").upper(),
    }
    if enhanced_line_check:
        params["enhanced_line_check"] = "true"
    url = f"{IPQS_ENDPOINT}/{urllib.parse.quote(phone)}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "IPQS-KEY": api_key,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def lookup_phone_intelligence(phone, *, now=None):
    normalized = normalize_phone(phone)
    if not normalized:
        return degraded("invalid", phone=phone)

    api_key = os.environ.get("IPQS_API_KEY") or os.environ.get("IPQUALITYSCORE_API_KEY")
    if not api_key:
        return degraded("not_configured", phone=normalized)

    strictness = os.environ.get("IPQS_PHONE_STRICTNESS", "0")
    country_hint = os.environ.get("IPQS_PHONE_COUNTRY_HINT", "US")
    enhanced = _bool(os.environ.get("IPQS_ENHANCED_LINE_CHECK"))
    policy = lookup_policy_version(country_hint=country_hint, strictness=strictness, enhanced_line_check=enhanced)

    cached = _cache_get(normalized, policy, now=now)
    if cached:
        return cached

    limit = _env_int("DIALPAD_PHONE_INTELLIGENCE_MAX_CALLS_PER_WINDOW", 120)
    if not budget_available("ipqs", normalized, limit, now=now):
        return degraded("budget_exceeded", phone=normalized)

    timeout = _env_float("IPQS_PHONE_TIMEOUT_SECONDS", 2.5)
    try:
        payload = _ipqs_request(normalized, api_key, timeout, country_hint, strictness, enhanced)
    except TimeoutError:
        return degraded("timeout", phone=normalized)
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            return degraded("timeout", phone=normalized)
        return degraded("unavailable", phone=normalized)
    except (ValueError, OSError, urllib.error.HTTPError):
        return degraded("unavailable", phone=normalized)

    result = normalize_ipqs_payload(normalized, payload)
    result["cache"] = {"hit": False, "policyVersion": policy}
    if result.get("status") in SAFE_STATUSES:
        _cache_set(normalized, policy, result, now=now)
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    phone = argv[-1] if argv else ""
    try:
        result = lookup_phone_intelligence(phone)
    except Exception:
        result = degraded("unavailable", phone=phone)
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
