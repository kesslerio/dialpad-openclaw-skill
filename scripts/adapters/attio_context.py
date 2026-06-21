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
    """Return the first *active* dict entry for ``key`` (Attio value envelope).

    Attio wraps each attribute as a list of typed objects carrying an
    active_from/active_until envelope. Historical/inactive entries (those with a
    non-null ``active_until``) can be listed BEFORE the active one, so blindly
    taking ``arr[0]`` can return a stale name/email. Prefer the first entry whose
    ``active_until`` is None; fall back to the first usable dict entry only when no
    explicitly-active one exists. Non-dict entries are skipped defensively.
    """
    arr = (values or {}).get(key)
    if not isinstance(arr, list) or not arr:
        return None
    fallback = None
    for entry in arr:
        if not isinstance(entry, dict):
            continue
        if entry.get("active_until") is None:
            return entry
        if fallback is None:
            fallback = entry
    return fallback


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


# --- people-field extraction (confirmed shapes; side-effect-free, reused by S2) ---
#
# Verified live 2026-06-18 against the people object:
#   values.name            -> [{"first_name", "last_name", "full_name", ...}]
#   values.email_addresses -> [{"email_address", "original_email_address", ...}]
#   values.company         -> [{"target_record_id", ...}] (direct ref; preferred)
# Every values.* is a list of objects with an active_from/active_until envelope;
# read the first active entry defensively and never assume a non-empty list.

def person_name_parts(person):
    """Return (first_name, last_name, full_name) from a person record, each cleaned or None.

    Tolerates ``name`` not being a list, ``[None]``, or missing sub-keys.
    """
    item = _first(((person or {}).get("values") or {}), "name")
    if not isinstance(item, dict):
        return (None, None, None)
    first = _clean(item.get("first_name")) if isinstance(item.get("first_name"), str) else None
    last = _clean(item.get("last_name")) if isinstance(item.get("last_name"), str) else None
    full = _clean(item.get("full_name")) if isinstance(item.get("full_name"), str) else None
    if not full and (first or last):
        full = " ".join(part for part in (first, last) if part) or None
    return (first, last, full)


def person_primary_email(person):
    """Return the person's primary email address (cleaned, lower-cased), or None.

    Tolerates ``email_addresses`` not being a list or holding non-dict entries.
    """
    item = _first(((person or {}).get("values") or {}), "email_addresses")
    if not isinstance(item, dict):
        return None
    email = item.get("email_address") or item.get("original_email_address")
    if isinstance(email, str) and "@" in email:
        cleaned = _clean(email)
        return cleaned.lower() if cleaned else None
    return None


def person_company_name(person):
    """Resolve a person's company name from the direct ``company`` ref.

    Prefers the direct ``company`` record-reference. Returns a cleaned name or
    None; never raises (``get_record`` swallows AttioError and returns None).
    """
    values = (person or {}).get("values") or {}
    company_id = _reference_id(values, "company")
    if company_id:
        company = get_record("companies", company_id)
        name = _text_value((company or {}).get("values"), "name")
        if name:
            return name
    return None


# --- resolution helpers (reusable by S2) ---

def find_person_by_phone(phone):
    """Return the first Attio person whose phone_numbers matches, or None."""
    if not phone:
        return None
    recs = _query_records("people", {"phone_numbers": phone}, limit=1)
    return recs[0] if recs else None


def find_people_by_phone(phone, limit=2):
    """Return up to ``limit`` Attio people matching ``phone`` (default 2).

    Used by the S5 note write-back to detect an AMBIGUOUS phone (a shared /
    recycled / family-plan number that resolves to more than one person). Writing a
    customer's SMS onto the wrong person's timeline is a cross-customer CRM leak, so
    the caller refuses to write when this returns more than one record. Returns a
    list (possibly empty); never raises beyond the underlying AttioError.
    """
    if not phone:
        return []
    return _query_records("people", {"phone_numbers": phone}, limit=limit)


def find_person_by_email(email):
    """Return the first Attio person whose email_addresses matches, or None."""
    if not email or "@" not in str(email):
        return None
    recs = _query_records("people", {"email_addresses": email}, limit=1)
    return recs[0] if recs else None


def person_record_id(person):
    """Return the Attio record_id for a person record, or None.

    VERIFIED live shape: ``person["id"]["record_id"]`` (sibling keys are
    ``workspace_id``, ``object_id``). Tolerates a non-dict person / missing id
    envelope without raising. This is the value Attio's ``POST /notes`` wants as
    ``parent_record_id`` (S5 person-timeline write-back).
    """
    ident = (person or {}).get("id") if isinstance(person, dict) else None
    if isinstance(ident, dict):
        rid = ident.get("record_id")
        return rid if isinstance(rid, str) and rid.strip() else None
    return None


def create_person_note(person, content, *, title="Inbound SMS"):
    """POST a plaintext note onto a person record's timeline. Returns the note id, or None.

    Fail-closed like every helper here: raises ``AttioError`` on transport/API
    failure (the S5 caller swallows it). Returns ``None`` — never raises — when
    there is no usable record id or no content (no POST is issued).

    Endpoint is the bare ``/notes``: ``ATTIO_BASE`` already ends in ``/v2`` and
    ``_request`` concatenates ``f"{ATTIO_BASE}{path}"`` (a ``/v2/notes`` path would
    resolve to ``/v2/v2/notes`` -> 404).

    Body is the canonical Attio create-note shape (verified against the Attio REST
    reference 2026-06): a ``data`` wrapper with ``parent_object`` / ``parent_record_id``
    / ``title`` (required) and ``content_plaintext`` (the live API uses
    ``content_plaintext`` / ``content_markdown`` — NOT a ``format``/``content`` pair).
    The created note id lives at ``data.id.note_id`` in the response.
    """
    record_id = person_record_id(person)
    if not record_id:
        return None
    text = _clean((content or "").strip()[:160])
    if not text:
        return None
    body = {
        "data": {
            "parent_object": "people",
            "parent_record_id": record_id,
            "title": title,
            "content_plaintext": text,
        }
    }
    resp = _request("POST", "/notes", body)
    note_id = None
    if isinstance(resp, dict):
        ident = (resp.get("data") or {}).get("id")
        if isinstance(ident, dict):
            note_id = ident.get("note_id")
    # A 2xx with an unexpected id envelope still means the note was created; return
    # a truthy sentinel so the caller records success rather than a silent no-op.
    return note_id or True


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
