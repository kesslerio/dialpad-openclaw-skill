"""Per-teammate inbound SMS alert forwarding (team-send-gate Phase 3).

A teammate's assistant registers a webhook through the team MCP
``set_sms_alert_url`` tool. When an inbound SMS reaches the webhook server,
this module POSTs a compact JSON notice to each registered caller whose
alert lines contain the receiving line, reusing the ops-wake guarantees:
minimal payload, SQLite dedupe, rate caps, background thread, never raises,
never delays the ACK.

Alert lines = the caller's own ``acts_for`` line plus the shared lines
operators grant it in the MCP config's ``alerts.callers`` section; every
granted line must already be in the caller's send-gate allowed set. The
support line never forwards.

Configuration lives in the shared MCP config (``DIALPAD_TEAM_MCP_CONFIG``,
a mode-0600/0640 JSON file) under ``alerts``::

    {"store": "~/.config/dialpad/team-alerts.json",
     "dedupe_db": "~/.config/dialpad/team-alerts.dedupe.db",   # optional
     "allowed_url_hosts": ["hooks.example.com"],
     "callers": {"team-scott": {"shared_lines": ["office"]}}}

``shared_lines`` entries may name a ``shared_lines`` key from the send-gate
config or be a literal number; resolving alert lines also needs
``DIALPAD_SEND_GATE_CONFIG`` on the webhook service.
"""

from __future__ import annotations

import ipaddress
import json
import os
import stat
import tempfile
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import ops_wake
import send_api_server
import send_gate

CONFIG_ENV = "DIALPAD_TEAM_MCP_CONFIG"
MAX_URL_CHARS = 2048
MAX_KEY_CHARS = 256


def _log(message: str) -> None:
    try:
        print(f"[{datetime.now().isoformat()}] team-alerts: {message}", flush=True)
    except Exception:  # noqa: BLE001 - logging must never raise.
        pass


def read_private_json(path: str | Path) -> Any:
    """Read JSON from a mode-0600/0640 file; anything weaker raises."""
    target = Path(path).expanduser()
    mode = stat.S_IMODE(os.stat(target).st_mode)
    if mode & 0o137:  # owner-x, group-wx, other-anything are all too open
        raise ValueError(f"{target} must be mode 0600 or 0640")
    return json.loads(target.read_text(encoding="utf-8"))


def write_private_json(path: str | Path, payload: Any) -> None:
    """Atomically replace `path` with JSON (tempfile + os.replace), mode 0600."""
    target = Path(path).expanduser()
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def validate_alerts_section(section: Any, *, gate_config: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalize the ``alerts`` block of the MCP config.

    Returns None when the block is absent (alerting off). Raises ValueError
    on any invalid shape or on a configured alert line outside the caller's
    send-gate allowed set — alert config fails closed.
    """
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ValueError("alerts must be an object")
    store = section.get("store")
    if not isinstance(store, str) or not store.strip():
        raise ValueError("alerts.store must be a non-empty path")
    hosts = section.get("allowed_url_hosts", [])
    if not isinstance(hosts, list) or any(not isinstance(h, str) or not h.strip() for h in hosts):
        raise ValueError("alerts.allowed_url_hosts must be a list of non-empty strings")
    dedupe_db = section.get("dedupe_db")
    if dedupe_db is not None and (not isinstance(dedupe_db, str) or not dedupe_db.strip()):
        raise ValueError("alerts.dedupe_db must be a non-empty path")
    callers_cfg = section.get("callers", {})
    if not isinstance(callers_cfg, dict):
        raise ValueError("alerts.callers must be an object")
    named_lines = gate_config.get("shared_lines") or {}
    normalized_callers: dict[str, dict[str, Any]] = {}
    for caller, entry in callers_cfg.items():
        allowed = send_gate.allowed_lines_for_caller(gate_config, caller)
        if not allowed:
            raise ValueError(f"alerts.callers: {caller!r} is not a send-gate caller with lines")
        if not isinstance(entry, dict):
            raise ValueError(f"alerts.callers.{caller} must be an object")
        extras = entry.get("shared_lines", [])
        if not isinstance(extras, list):
            raise ValueError(f"alerts.callers.{caller}.shared_lines must be a list")
        resolved: set[str] = set()
        for item in extras:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"alerts.callers.{caller}.shared_lines entries must be strings")
            candidate = named_lines.get(item.strip(), item.strip())
            canonical = send_gate._canonical_sender(candidate)
            if canonical not in allowed or send_api_server._is_support_line(canonical):
                raise ValueError(f"alerts.callers.{caller} line {item!r} is not allowed for this caller")
            resolved.add(canonical)
        normalized_callers[caller] = {"shared_lines": resolved}
    return {
        "store": store.strip(),
        "allowed_url_hosts": [host.strip().lower().lstrip(".") for host in hosts],
        "dedupe_db": dedupe_db.strip() if isinstance(dedupe_db, str) else None,
        "callers": normalized_callers,
    }


def alert_lines_for_caller(
    caller: str,
    *,
    gate_config: Mapping[str, Any],
    alerts: Mapping[str, Any],
) -> set[str]:
    """Lines whose inbound SMS alerts this caller: the own ``acts_for`` line
    plus validated ``alerts.callers`` shared lines. Never the support line."""
    entry = (gate_config.get("callers") or {}).get(caller)
    if not isinstance(entry, dict):
        return set()
    lines: set[str] = set()
    acts_for = entry.get("acts_for")
    if isinstance(acts_for, str):
        own = (gate_config.get("teammates") or {}).get(acts_for)
        if own:
            lines.add(send_gate._canonical_sender(own))
    extra = (alerts.get("callers") or {}).get(caller) or {}
    lines.update(extra.get("shared_lines") or set())
    return {line for line in lines if not send_api_server._is_support_line(line)}


def validate_alert_url(url: Any, *, allowed_hosts: list[str]) -> str:
    """Validate a teammate alert endpoint. https only, with http permitted
    only for loopback (tests); userinfo and non-loopback IP literal hosts are
    rejected. Returns the stripped URL or raises ValueError."""
    text = str(url or "").strip()
    if not text or len(text) > MAX_URL_CHARS:
        raise ValueError(f"url must be a non-empty string of at most {MAX_URL_CHARS} characters")
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError as exc:
        raise ValueError("url is not parseable") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url must not contain userinfo")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("url must have a host")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if parsed.scheme == "http":
        if host != "localhost" and not (literal is not None and literal.is_loopback):
            raise ValueError("http alert urls are allowed only for loopback")
        return text
    if parsed.scheme != "https":
        raise ValueError("url must use https")
    if literal is not None:
        raise ValueError("url host must be a name, not an IP literal")
    if allowed_hosts and not any(host == suffix or host.endswith("." + suffix) for suffix in allowed_hosts):
        raise ValueError("url host is not in alerts.allowed_url_hosts")
    return text


def load_alert_store(path: str | Path, *, strict: bool = False) -> dict[str, Any]:
    """``{caller: {url, key, updated_at}}``; missing reads as empty, and a
    corrupt store reads as empty for forwarding but raises when strict
    (registration writes must not silently clobber other callers)."""
    try:
        raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        if strict:
            raise ValueError(f"alert store unreadable at {path}: {exc}") from exc
        _log(f"alert store unreadable ({type(exc).__name__}); treating as empty")
        return {}
    if not isinstance(raw, dict):
        if strict:
            raise ValueError(f"alert store at {path} must be a JSON object")
        _log("alert store is not a JSON object; treating as empty")
        return {}
    return raw


def set_alert_registration(store_path: str | Path, caller: str, url: str, key: str) -> None:
    """Persist one caller's alert endpoint; an empty url clears it."""
    path = Path(store_path).expanduser()
    entries = load_alert_store(path, strict=True)
    if url:
        entries[caller] = {
            "url": url,
            "key": key,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    else:
        entries.pop(caller, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private_json(path, entries)


def _alerts_config(environ: Mapping[str, str]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(validated alerts section, gate config); any failure disables forwarding."""
    raw_path = (environ.get(CONFIG_ENV) or "").strip()
    if not raw_path:
        return None, None
    try:
        raw = read_private_json(raw_path)
        gate_config = send_gate.load_config_from_env(environ)
        section = validate_alerts_section(
            raw.get("alerts") if isinstance(raw, dict) else None,
            gate_config=gate_config,
        )
    except Exception as exc:  # noqa: BLE001 - a config failure fails closed.
        _log(f"alerts config unavailable ({type(exc).__name__}); forwarding disabled")
        return None, None
    return section, gate_config


def _targets(environ: Mapping[str, str]) -> tuple[dict[str, dict[str, Any]], str | None]:
    """{caller: {url, key, alert_lines}} for every registered caller, plus the
    dedupe db path (default: a sibling of the alert store)."""
    alerts, gate_config = _alerts_config(environ)
    if not alerts or not gate_config:
        return {}, None
    store = load_alert_store(alerts["store"])
    targets: dict[str, dict[str, Any]] = {}
    for caller, registration in store.items():
        if not isinstance(registration, dict):
            continue
        key = registration.get("key")
        try:
            url = validate_alert_url(registration.get("url"), allowed_hosts=alerts["allowed_url_hosts"])
        except ValueError:
            continue
        if not isinstance(key, str) or not key.strip():
            continue
        lines = alert_lines_for_caller(caller, gate_config=gate_config, alerts=alerts)
        if lines:
            targets[caller] = {"url": url, "key": key, "alert_lines": lines}
    dedupe_db = alerts["dedupe_db"] or f"{alerts['store']}.dedupe.db"
    return targets, str(Path(dedupe_db).expanduser())


def _deliver(wake: dict[str, Any], caller: str, registration: Mapping[str, Any], *, dedupe_db: str | None) -> bool:
    """Claim, rate-check, and POST one caller's alert. Never raises."""
    try:
        dedupe_key = f"team-alert:{caller}:{wake['dedupe_key']}"
        if not ops_wake.claim_event(dedupe_key, db_path=dedupe_db):
            return False
        allowed, suppressed = ops_wake._take_rate_slot()
        if not allowed:
            _log(f"{caller} rate-capped; will report on next wake")
            return False
        payload = dict(wake["payload"])
        payload["sent_at"] = ops_wake._now_iso()
        payload["suppressed_since_last"] = suppressed
        return ops_wake.post_wake(payload, url=registration["url"], key=registration["key"])
    except Exception as exc:  # noqa: BLE001 - never break the receiver.
        _log(f"{caller} alert skipped ({type(exc).__name__})")
        return False


def _dispatch(wake: dict[str, Any], *, environ: Mapping[str, str] | None) -> bool:
    """Resolve targets and deliver; True when at least one POST went out."""
    try:
        env = os.environ if environ is None else environ
        line = (wake.get("payload") or {}).get("line")
        targets, dedupe_db = _targets(env)
        posted = False
        for caller, registration in targets.items():
            if line in registration["alert_lines"]:
                posted = _deliver(wake, caller, registration, dedupe_db=dedupe_db) or posted
        return posted
    except Exception as exc:  # noqa: BLE001 - never break the receiver.
        _log(f"alert dispatch skipped ({type(exc).__name__})")
        return False


def notify_sms_event(data: Any, *, background: bool = True, environ: Mapping[str, str] | None = None) -> bool:
    """Forward a minimal inbound-SMS notice to each registered caller whose
    alert lines contain the receiving line. Same contract as
    ``ops_wake.notify_sms_event``: never raises, never delays the ACK."""
    try:
        wake = ops_wake.build_sms_wake(data)
        if not wake:
            return False
        line = (wake.get("payload") or {}).get("line")
        if not line or send_api_server._is_support_line(line):
            return False
        if not background:
            return _dispatch(wake, environ=environ)
        threading.Thread(
            target=_dispatch,
            args=(wake,),
            kwargs={"environ": environ},
            daemon=True,
            name="team-alerts",
        ).start()
        return True
    except Exception as exc:  # noqa: BLE001 - never break the receiver.
        _log(f"alert skipped ({type(exc).__name__})")
        return False
