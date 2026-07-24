"""Allow NANP destinations and reject non-NANP outbound Dialpad actions.

NANP includes every +1 territory, not only the United States and Canada.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


_NANP_E164_RE = re.compile(r"^\+1[0-9]{10}$")
_NANP_NATIONAL_RE = re.compile(r"^[0-9]{10}$")
_UNSUPPORTED_MESSAGE = (
    "Outbound Dialpad SMS and calls support NANP (+1) destinations only; "
    "non-NANP international destinations are not supported."
)


def is_supported_outbound_destination(
    phone_number: str,
    *,
    allow_nanp_national: bool = False,
) -> bool:
    """Return whether a destination is a NANP number in canonical E.164 form."""
    normalized = str(phone_number)
    return bool(
        _NANP_E164_RE.fullmatch(normalized)
        or (allow_nanp_national and _NANP_NATIONAL_RE.fullmatch(normalized))
    )


def require_supported_outbound_destinations(
    phone_numbers: Iterable[str],
    *,
    allow_nanp_national: bool = False,
) -> None:
    """Reject the whole action when any destination is outside the NANP."""
    normalize_supported_outbound_destinations(
        phone_numbers,
        allow_nanp_national=allow_nanp_national,
    )


def normalize_supported_outbound_destinations(
    phone_numbers: Iterable[str],
    *,
    allow_nanp_national: bool = False,
) -> list[str]:
    """Return explicit NANP E.164 destinations or reject the whole action."""

    normalized: list[str] = []
    for number in phone_numbers:
        candidate = str(number)
        if _NANP_E164_RE.fullmatch(candidate):
            normalized.append(candidate)
        elif allow_nanp_national and _NANP_NATIONAL_RE.fullmatch(candidate):
            normalized.append(f"+1{candidate}")
        else:
            raise ValueError(_UNSUPPORTED_MESSAGE)
    return normalized
