"""Optional model wording layer for Dialpad approval drafts.

The webhook owns evidence retrieval. This module owns only the final wording
command contract: compact facts in, safe SMS draft out, deterministic fallback
on every failure.
"""

import json
import re
import shlex
import subprocess
from dataclasses import dataclass


URL_RE = re.compile(
    r"(?:https?://|www\.)\S+|(?:[a-z0-9-]+\.)+"
    r"(?:com|net|org|io|co|ai|app|ly|me|edu|gov|health|clinic)(?:/\S*)?",
    re.IGNORECASE,
)
UNSAFE_OUTPUT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bqmd://",
        r"\bScore:\s*\d",
        r"\bContext:",
        r"\bTitle:",
        r"@@\s+-\d",
        r"/home/",
        r"\.md\b",
        r"\.json\b",
    )
)
UNSUPPORTED_SCHEDULE_CLAIM_RE = re.compile(
    r"\b(?:is|was|'s)\s+scheduled\b|\bscheduled\s+(?:for|at|on)\b|"
    r"\b(?:demo|meeting|call|appointment)\s+(?:is|'s|was)?\s*(?:today|tomorrow|on|at|for)\b|"
    r"\b(?:you(?:'re| are)?|we(?:'re| are)?)\s+booked\b|\bbooked\s+(?:for|on|at|today|tomorrow)\b",
    re.IGNORECASE,
)
RAW_COMMS_CLAIM_RE = re.compile(
    r"\b(?:i|we)\s+(?:read|saw|found)\s+(?:your\s+)?(?:email|gmail|sms|text)\b",
    re.IGNORECASE,
)
INTERNAL_SOURCE_NAME_RE = re.compile(r"\b(?:attio|crm|gmail|qmd|provenance)\b", re.IGNORECASE)
LOW_CONF_PERSONAL_GREETING_RE = re.compile(r"^\s*(?:hi|hello|hey)\s+(?!there\b)[^,!.]{2,40}[,!.]?", re.IGNORECASE)
PUBLIC_LOOKUP_CLAIM_RE = re.compile(
    r"\b(?:looked you up|found you online|saw your business|your company|your organization|your role)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DraftModelConfig:
    command: str = ""
    timeout_seconds: float = 4
    max_chars: int = 320
    approved_booking_url: str = "https://bysha.pe/book-demo"


def base_draft_basis(basis):
    text = str(basis or "")
    if text.startswith("model_"):
        return text[len("model_"):]
    return text


def _event_type(normalized_event):
    return normalized_event.get("event_type") or "sms"


def _is_missed_call_event(normalized_event):
    return _event_type(normalized_event) == "missed_call"


def _compact_scalar(value, limit=120):
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text[:limit]


def _compact_dict(data, keys):
    if not isinstance(data, dict):
        return {}
    compact = {}
    for key in keys:
        value = _compact_scalar(data.get(key), limit=240)
        if value is not None:
            compact[key] = value
    return compact


def _facts(normalized_event, fallback_message, category, payload, greeting, config):
    inbound_context = normalized_event.get("inbound_context") or {}
    identity_confidence = inbound_context.get("identityConfidence")
    payload = payload if isinstance(payload, dict) else {}
    facts = {
        "task": "draft_operator_reviewed_sales_sms",
        "constraints": [
            "Write one concise SMS under the max character limit.",
            "Use only the supplied facts.",
            "Do not invent scheduled meetings, replies, prices, or commitments.",
            "Do not mention internal tools, CRM, Gmail, Attio, QMD, or provenance.",
            "Do not quote raw email or SMS bodies.",
            "Treat phone validation and public prospect facts as low-confidence operator evidence.",
            "Do not greet by reverse-lookup name or mention an unconfirmed company/business.",
            "Return JSON like {\"message\":\"...\"}.",
        ],
        "maxChars": config.max_chars,
        "fallbackMessage": fallback_message,
        "event": {
            "type": _event_type(normalized_event),
            "isMissedCall": _is_missed_call_event(normalized_event),
            "lineDisplay": normalized_event.get("line_display"),
            "customerText": _compact_scalar(normalized_event.get("text"), limit=240),
            "identityConfidence": identity_confidence,
            "category": category,
        },
        "candidate": _compact_dict(payload, ("basis", "category", "message")),
        "recipient": {"greetingName": greeting},
        "sources": {
            "crm": _compact_dict(
                normalized_event.get("crm_context"),
                ("company", "deal", "stage", "summary"),
            ) if identity_confidence == "high" else {},
            "calendar": _compact_dict(
                normalized_event.get("calendar_context"),
                ("status", "basis", "summary", "demoState"),
            ) if identity_confidence == "high" else {},
            "comms": _compact_dict(
                normalized_event.get("comms_context"),
                ("status", "basis", "summary", "smsStatus", "gmailStatus"),
            ) if identity_confidence == "high" else {},
        },
    }
    caller_intelligence = inbound_context.get("callerIntelligence") or normalized_event.get("caller_intelligence")
    if isinstance(caller_intelligence, dict):
        phone = caller_intelligence.get("phone") if isinstance(caller_intelligence.get("phone"), dict) else {}
        line = caller_intelligence.get("line") if isinstance(caller_intelligence.get("line"), dict) else {}
        risk = caller_intelligence.get("risk") if isinstance(caller_intelligence.get("risk"), dict) else {}
        possible = caller_intelligence.get("possibleIdentity") if isinstance(caller_intelligence.get("possibleIdentity"), dict) else {}
        public = caller_intelligence.get("publicProspect") if isinstance(caller_intelligence.get("publicProspect"), dict) else {}
        facts["sources"]["callerIntelligence"] = {
            "status": _compact_scalar(caller_intelligence.get("status"), limit=40),
            "phone": _compact_dict(phone, ("country", "region", "city")),
            "line": _compact_dict(line, ("carrier", "type", "activeStatus")),
            "risk": _compact_dict(risk, ("level",)),
            "possibleIdentity": _compact_dict(possible, ("basis", "confidence")),
            "publicProspect": _compact_dict(public, ("status", "summary", "confidence")),
        }
    rich = normalized_event.get("rich_reply")
    if isinstance(rich, dict):
        facts["sources"]["currentDraftBasis"] = _compact_dict(
            rich,
            ("basis", "category", "message"),
        )
    return facts


def _extract_message(output):
    text = str(output or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(payload, dict) or payload.get("usable") is False:
        return None
    for key in ("message", "draft", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _customer_safe_text(output):
    text = " ".join(str(output or "").split())
    if not text:
        return ""
    for pattern in UNSAFE_OUTPUT_PATTERNS:
        if pattern.search(text):
            return ""
    return text


def _approved_url_only(text, config):
    approved = str(config.approved_booking_url or "").rstrip("/")
    for url in URL_RE.findall(text or ""):
        clean = url.rstrip(".,;)")
        if not re.match(r"https?://", clean, re.IGNORECASE):
            clean = f"https://{clean}"
        clean_lower = clean.lower()
        approved_lower = approved.lower()
        if clean_lower == approved_lower or clean_lower.startswith(f"{approved_lower}/"):
            continue
        if clean_lower.startswith(f"{approved_lower}?"):
            continue
        return False
    return True


def _safe_message(text, normalized_event, config, greeting):
    message = " ".join(str(text or "").split())
    if not message or len(message) > config.max_chars:
        return None
    identity_confidence = (normalized_event.get("inbound_context") or {}).get("identityConfidence")
    if identity_confidence != "high":
        expected = str(greeting or "there").strip().lower()
        if expected != "there" or LOW_CONF_PERSONAL_GREETING_RE.search(message):
            return None
    if not _customer_safe_text(message):
        return None
    if not _approved_url_only(message, config):
        return None
    calendar_context = normalized_event.get("calendar_context")
    calendar_usable = isinstance(calendar_context, dict) and calendar_context.get("usable")
    demo_state = calendar_context.get("demoState") if isinstance(calendar_context, dict) else None
    calendar_supports_scheduled_claim = bool(calendar_usable and demo_state != "recent")
    if not calendar_supports_scheduled_claim and UNSUPPORTED_SCHEDULE_CLAIM_RE.search(message):
        return None
    if RAW_COMMS_CLAIM_RE.search(message):
        return None
    if INTERNAL_SOURCE_NAME_RE.search(message):
        return None
    if identity_confidence != "high" and PUBLIC_LOOKUP_CLAIM_RE.search(message):
        return None
    return message


def _run_model(facts, config):
    try:
        args = shlex.split(str(config.command or "").strip())
    except ValueError:
        return None, "invalid_command"
    if not args:
        return None, "disabled"
    try:
        completed = subprocess.run(
            args,
            input=json.dumps(facts, separators=(",", ":")),
            check=False,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
        )
    except FileNotFoundError:
        return None, "unavailable"
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as exc:  # noqa: BLE001 - model drafting must fail closed.
        print(f"WARNING: Draft model failed ({type(exc).__name__})")
        return None, "request_failed"
    if completed.returncode != 0:
        return None, f"exit_{completed.returncode}"
    return _extract_message(completed.stdout), "ok"


def apply_model_draft(normalized_event, payload, config, greeting):
    if not str(config.command or "").strip():
        return payload
    fallback = payload.get("message")
    if not fallback:
        return payload

    facts = _facts(
        normalized_event,
        fallback,
        payload.get("category"),
        payload,
        greeting,
        config,
    )
    candidate, status = _run_model(facts, config)
    message = _safe_message(candidate, normalized_event, config, greeting)
    if status == "ok" and not message:
        status = "unsafe_output"

    payload["modelDraft"] = {
        "status": status,
        "basis": "draft_model" if message else None,
    }
    if not message:
        return payload

    payload["message"] = message
    payload["modelDraft"]["basis"] = "draft_model"
    payload["modelDraft"]["fallbackBasis"] = payload.get("basis")
    payload["basis"] = f"model_{payload.get('basis') or 'draft'}"
    return payload
