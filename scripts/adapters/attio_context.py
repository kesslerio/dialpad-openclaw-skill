#!/usr/bin/env python3
"""Attio CRM context adapter for the Dialpad auto-responder (S1/U2).

Resolves an inbound sender to compact Attio CRM context for the CRM-aware draft
mode in ``webhook_server.py``. Wired as a context command:

    DIALPAD_CRM_CONTEXT_COMMAND="/abs/python3 /abs/scripts/adapters/attio_context.py"

The webhook appends the query as a single final CLI arg, space-joined:

    "<sender_number> <name> <company>"

Emits a JSON object on stdout matching the contract consumed by
``lookup_sales_crm_context``:

    {"usable": true, "status": "ok", "basis": "attio",
     "summary": "...", "deal": "...", "stage": "...",
     "company": "...", "owner": null}

On any miss or error it emits ``{"usable": false, "status": "..."}`` and exits 0
(the webhook treats a non-zero exit as failure). It never raises to the caller.

The resolution helpers (``find_person_by_phone``, ``find_person_by_email``,
``deal_for_person``) are the reusable Attio client for the S2 phone-first
identity resolver — keep them import-safe and side-effect-free.

Schema reference: docs/reference/attio-schema.md
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

ATTIO_BASE = os.environ.get("ATTIO_API_BASE", "https://api.attio.com/v2").rstrip("/")
HTTP_TIMEOUT = float(os.environ.get("ATTIO_HTTP_TIMEOUT_SECONDS", "2.5"))

# E.164-ish: leading + or digit, then digits/separators. The sender_number the
# webhook passes first is the reliable lookup key; name/company are hints only.
_PHONE_RE = re.compile(r"^\+?\d[\d()\-.\s]{5,}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clean(text):
    """Strip control chars and collapse whitespace on CRM-sourced strings.

    Defense in depth: these values can reach a customer-facing SMS draft. The
    webhook also applies customer_safe_knowledge_text downstream, but the adapter
    does not rely on that alone.
    """
    if not isinstance(text, str):
        return text
    return " ".join(_CONTROL_RE.sub(" ", text).split()) or None


def _normalize_phone(token):
    """Reduce a phone token to '+' + digits so it matches Attio's E.164 storage."""
    digits = re.sub(r"\D", "", token or "")
    return ("+" + digits) if str(token).strip().startswith("+") else digits


class AttioError(Exception):
    """Raised on an Attio API/transport failure. Adapters fail closed on it."""


def _api_key():
    return os.environ.get("ATTIO_API_KEY", "")


def _request(method, path, body=None):
    if not _api_key():
        raise AttioError("missing_api_key")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{ATTIO_BASE}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise AttioError(f"http_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise AttioError("network") from exc


def _query_records(object_slug, filt, limit=1):
    body = {"limit": limit}
    if filt:
        body["filter"] = filt
    return _request("POST", f"/objects/{object_slug}/records/query", body).get("data") or []


def get_record(object_slug, record_id):
    """GET a single record; returns the record dict or None (never raises)."""
    if not record_id:
        return None
    try:
        return _request("GET", f"/objects/{object_slug}/records/{record_id}").get("data")
    except AttioError:
        return None


# --- value extraction (Attio wraps each attribute as a list of typed objects) ---

def _first(values, key):
    arr = (values or {}).get(key)
    return arr[0] if isinstance(arr, list) and arr else None


def _text_value(values, key):
    item = _first(values, key)
    if isinstance(item, dict):
        val = item.get("value")
        return _clean(val) if isinstance(val, str) and val.strip() else None
    return None


def _status_title(values, key="stage"):
    item = _first(values, key)
    if isinstance(item, dict):
        status = item.get("status") or {}
        title = status.get("title")
        return _clean(title) if isinstance(title, str) and title.strip() else None
    return None


def _reference_id(values, key):
    item = _first(values, key)
    if isinstance(item, dict):
        return item.get("target_record_id")
    return None


# --- resolution helpers (reusable by S2) ---

def find_person_by_phone(phone):
    """Return the first Attio person whose phone_numbers matches, or None."""
    if not phone:
        return None
    recs = _query_records("people", {"phone_numbers": phone}, limit=1)
    return recs[0] if recs else None


def find_person_by_email(email):
    """Return the first Attio person whose email_addresses matches, or None."""
    if not email or "@" not in str(email):
        return None
    recs = _query_records("people", {"email_addresses": email}, limit=1)
    return recs[0] if recs else None


def deal_for_person(person):
    """Return the first deal record linked via the person's associated_deals, else None."""
    values = (person or {}).get("values") or {}
    for ref in values.get("associated_deals") or []:
        if not isinstance(ref, dict):
            continue
        deal = get_record("deals", ref.get("target_record_id"))
        if deal:
            return deal
    return None


def _company_name(deal_values):
    return _text_value((get_record("companies", _reference_id(deal_values, "associated_company")) or {}).get("values"), "name")


def crm_context_from_records(person, deal):
    """Build the CRM context contract from a resolved person + deal.

    Usable only when there is real CRM substance (company, deal, or stage);
    a bare person match with no deal context returns ``{"usable": false}``.
    """
    deal_values = (deal or {}).get("values") or {}
    deal_name = _text_value(deal_values, "name")
    stage = _status_title(deal_values, "stage")
    company = _company_name(deal_values) if deal else None

    if not (company or deal_name or stage):
        return {"usable": False, "status": "no_context"}

    summary = " · ".join(
        part for part in (
            company,
            f"deal: {deal_name}" if deal_name else None,
            f"stage: {stage}" if stage else None,
        ) if part
    )
    return {
        "usable": True,
        "status": "ok",
        "basis": "attio",
        "summary": summary[:300],
        "deal": deal_name,
        "stage": stage,
        "company": company,
        "owner": None,  # owner is an actor_id; name resolution needs a scope this key lacks
    }


def _parse_query(query):
    """Split the space-joined query into (phone, remainder). Phone is the first token when phone-shaped."""
    tokens = str(query or "").split()
    if tokens and _PHONE_RE.match(tokens[0]):
        return tokens[0], " ".join(tokens[1:])
    return None, " ".join(tokens)


def build_crm_context(query):
    """Resolve the CRM context contract for a webhook context-command query."""
    phone, _rest = _parse_query(query)
    phone = _normalize_phone(phone) if phone else None
    if not phone:
        return {"usable": False, "status": "empty_query"}
    try:
        person = find_person_by_phone(phone)
    except AttioError as exc:
        return {"usable": False, "status": "degraded", "basis": "attio", "detail": str(exc)}
    if not person:
        return {"usable": False, "status": "not_found"}
    try:
        deal = deal_for_person(person)
    except AttioError:
        deal = None
    return crm_context_from_records(person, deal)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    query = argv[-1] if argv else ""
    try:
        result = build_crm_context(query)
    except Exception:  # noqa: BLE001 - fail closed; the adapter must never break the webhook
        result = {"usable": False, "status": "error"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
