#!/usr/bin/env python3
"""Phone-first identity resolver for the Dialpad SMS enrichment program (S2/U?).

A standalone, importable library that resolves an inbound sender to a compact
identity using a cheap -> expensive cascade, short-circuiting on a confident hit
and recording each step in ``sources[]``.

Single entrypoint::

    resolve_identity(phone, dialpad_contact=None, email=None) -> {
        "identity": {"name", "first_name", "last_name", "email", "company"},
        "confidence": "high" | "medium" | "low",
        "sources": [...],
    }

Cascade (ordered; a future unit can append later stages such as Apollo / Gmail /
reverse-phone — none of those clients exist in this repo, so they are NOT built
here):

    1. ``dialpad_contact`` dict passed by the caller. This library NEVER calls
       Dialpad over HTTP itself; the caller owns that lookup. Keeping the import
       network-free except for Attio is a hard requirement.
    2. Attio phone match via ``attio_context.find_person_by_phone``.
    3. Attio email match via ``attio_context.find_person_by_email`` — only once
       an email is known from the caller-supplied ``email`` arg or step 1/2.

Confidence vocabulary mirrors the webhook's existing high/medium/low identity
confidence:

    high   = exact Attio phone match that resolves a usable name.
    medium = Dialpad-contact name only (no Attio corroboration), OR an Attio
             person matched but yielded no usable name.
    low    = nothing resolved, or Attio degraded / errored.

Fail-closed discipline (mirrors ``attio_context``'s ``AttioError`` handling):
Attio failures are recorded as an ``attio_error`` source, never elevate
confidence, and never propagate an exception to the caller. With ``ATTIO_API_KEY``
unset, ``resolve_identity`` makes zero network calls and returns confidence
``low`` without raising.

WRONG-MATCH WARNING (for S3 and any customer-facing consumer):
A ``high`` result here is a single ``limit=1`` Attio phone match with NO collision
/ wrong-match check. That is acceptable in S2 because this unit produces no
customer-facing text. S3 MUST NOT auto-promote a ``high`` resolver result into a
customer-facing name or company (greeting, draft body, etc.) without re-applying
the wrong-match guard described in
``docs/solutions/ungate-enrichment-customer-pii.md`` (a reused / ported / shared
number or a stale Attio record can match the wrong person at any confidence).
"""
import os
import sys

# Make the sibling adapter importable whether this file is imported as a module
# or run from an arbitrary cwd. attio_context is the only Attio client we touch.
_ADAPTER_DIR = os.path.dirname(os.path.abspath(__file__))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

import attio_context  # noqa: E402

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"


def _empty_identity():
    return {
        "name": None,
        "first_name": None,
        "last_name": None,
        "email": None,
        "company": None,
    }


def _clean_str(value):
    """Sanitize a caller-supplied value, returning a clean string or None.

    ``attio_context._clean`` passes non-strings through unchanged, so guard the
    type here: malformed Dialpad payloads (ints, lists, dicts) must never leak a
    non-string into the identity contract or reach a ``str.join``.
    """
    if not isinstance(value, str):
        return None
    return attio_context._clean(value)


def _identity_from_dialpad(contact):
    """Extract a partial identity from a caller-supplied Dialpad contact dict.

    Returns (identity, has_name). Tolerates a non-dict ``contact`` and missing
    keys without raising. Accepts a handful of common name spellings so the
    caller is not forced into one exact shape.
    """
    identity = _empty_identity()
    if not isinstance(contact, dict):
        return identity, False

    first = _clean_str(contact.get("first_name"))
    last = _clean_str(contact.get("last_name"))
    name = (
        _clean_str(contact.get("name"))
        or _clean_str(contact.get("display_name"))
        or _clean_str(contact.get("full_name"))
    )
    if not name and (first or last):
        name = " ".join(part for part in (first, last) if part) or None

    # Raw Dialpad contacts carry an ``emails`` array (first entry primary) as well
    # as (sometimes) a singular ``email``. Prefer the singular field, then fall
    # back to the first array entry, so either shape seeds the Attio email stage.
    email = contact.get("email")
    if not (isinstance(email, str) and "@" in email):
        emails = contact.get("emails")
        if isinstance(emails, list) and emails:
            email = emails[0]
    if isinstance(email, str) and "@" in email:
        cleaned_email = _clean_str(email)
        identity["email"] = cleaned_email.lower() if cleaned_email else None

    company = _clean_str(contact.get("company"))

    identity["first_name"] = first
    identity["last_name"] = last
    identity["name"] = name
    identity["company"] = company
    return identity, bool(name)


def _merge_attio_person(identity, person):
    """Overlay an Attio person's name/email/company onto ``identity`` in place.

    Attio is the more authoritative source for the matched record, so its fields
    win where present; caller-supplied fields fill any gaps. Returns
    ``attio_has_name``: whether *Attio* (not a pre-existing Dialpad name) yielded
    a usable name. A high promotion must hinge on Attio corroboration, so an Attio
    person matched with no usable name stays medium even if Dialpad already named
    the identity — and the email follow-up still gets a chance to run.
    Never raises — ``person_company_name`` swallows AttioError internally.
    """
    first, last, full = attio_context.person_name_parts(person)
    if full:
        identity["name"] = full
    if first:
        identity["first_name"] = first
    if last:
        identity["last_name"] = last

    email = attio_context.person_primary_email(person)
    if email:
        identity["email"] = email

    try:
        company = attio_context.person_company_name(person)
    except attio_context.AttioError:
        company = None
    if company:
        identity["company"] = company

    return bool(full)


def resolve_identity(phone, dialpad_contact=None, email=None):
    """Resolve an inbound sender to a compact identity via a cheap->expensive cascade.

    Args:
        phone: the inbound sender's phone number (the primary lookup key).
        dialpad_contact: optional dict the caller already fetched from Dialpad.
            This library does NOT call Dialpad itself.
        email: optional email the caller already knows; seeds the Attio email
            stage even before an Attio person is matched.

    Returns a dict with ``identity``, ``confidence`` (high/medium/low), and an
    ordered ``sources`` list recording each cascade step. Never raises — a
    numeric/odd ``phone`` (or ``email``) degrades to a low-confidence result
    rather than raising.

    ``sources`` vocabulary: ``dialpad_contact`` / ``dialpad_contact_empty`` (a
    contact was supplied), ``attio_phone`` / ``attio_email`` (a person matched),
    ``attio_phone_not_found`` / ``attio_email_not_found`` (the lookup ran but
    matched nothing — provenance for a real miss), ``attio_error`` (fail-closed),
    and ``no_input`` ONLY when there was genuinely no phone, no email, and no
    usable dialpad_contact to resolve from.

    See the module docstring for the wrong-match warning that S3 must honor
    before putting any of this into customer-facing text.
    """
    identity = _empty_identity()
    sources = []
    confidence = CONFIDENCE_LOW

    # Seed a known email from the caller so the Attio email stage can run even if
    # the Dialpad contact carried no email and the phone match misses.
    if isinstance(email, str) and "@" in email:
        cleaned = _clean_str(email)
        if cleaned:
            identity["email"] = cleaned.lower()

    # --- Stage 1: caller-supplied Dialpad contact (cheapest; no I/O) ----------
    has_name = False
    if dialpad_contact is not None:
        dp_identity, dp_has_name = _identity_from_dialpad(dialpad_contact)
        for key, value in dp_identity.items():
            if value and not identity.get(key):
                identity[key] = value
        has_name = dp_has_name
        if dp_has_name:
            # A Dialpad name alone (no Attio corroboration yet) is medium.
            confidence = CONFIDENCE_MEDIUM
            sources.append("dialpad_contact")
        else:
            sources.append("dialpad_contact_empty")

    # --- Stage 2: Attio phone match ------------------------------------------
    # Fail closed on any Attio error: record it, do not elevate confidence, keep
    # whatever the Dialpad stage produced. With ATTIO_API_KEY unset, the adapter
    # raises AttioError("missing_api_key") BEFORE any network call, so this stays
    # network-free and degrades to the caller-supplied data.
    # Coerce a non-string phone (e.g. a numeric Dialpad payload) to a safe string
    # so _normalize_phone's re.sub never sees a non-str and raises. A blank/odd
    # value degrades to "no phone lookup" rather than crashing this entrypoint.
    phone_str = phone if isinstance(phone, str) else (str(phone) if phone is not None else None)
    attempted_phone = bool(phone_str and phone_str.strip())
    person = None
    if attempted_phone:
        try:
            normalized = attio_context._normalize_phone(phone_str)
            if normalized:
                person = attio_context.find_person_by_phone(normalized)
                if person is None:
                    sources.append("attio_phone_not_found")
        except attio_context.AttioError:
            sources.append("attio_error")
            person = None

    if person is not None:
        attio_has_name = _merge_attio_person(identity, person)
        sources.append("attio_phone")
        if attio_has_name:
            confidence = CONFIDENCE_HIGH
            has_name = True
        else:
            # Person matched but no usable name -> medium (never downgrade a name
            # we already had from Dialpad).
            has_name = has_name or False
            if confidence != CONFIDENCE_HIGH:
                confidence = CONFIDENCE_MEDIUM

    # --- Stage 3: Attio email match ------------------------------------------
    # Only runs once an email is known (from the caller, Dialpad, or the phone
    # match) AND we have not already resolved a high-confidence name from phone.
    attempted_email = bool(confidence != CONFIDENCE_HIGH and identity.get("email"))
    if attempted_email:
        try:
            email_person = attio_context.find_person_by_email(identity["email"])
        except attio_context.AttioError:
            sources.append("attio_error")
            email_person = None
        else:
            if email_person is None:
                sources.append("attio_email_not_found")
        if email_person is not None:
            email_has_name = _merge_attio_person(identity, email_person)
            sources.append("attio_email")
            if email_has_name:
                confidence = CONFIDENCE_HIGH
                has_name = True
            elif confidence != CONFIDENCE_HIGH:
                confidence = CONFIDENCE_MEDIUM

    # ``no_input`` means there was genuinely nothing to resolve from: no phone, no
    # email, and no usable dialpad_contact. A real miss (a lookup ran but found
    # nothing) is already recorded by its own *_not_found source above, so it must
    # not collapse to ``no_input`` and lose that provenance.
    if not sources:
        sources.append("no_input")

    return {"identity": identity, "confidence": confidence, "sources": sources}
