"""
Optional SMS security filter compatibility layer.

Provides no-op fallbacks when sms_security_filter module is not installed,
so consumers can import unconditionally without try/except boilerplate.

Install sms_security_filter (from shapescale-openclaw-skills/security)
to enable filtering. Without it, all messages pass through unchanged.
"""

try:
    from sms_security_filter import (
        is_sensitive_message,
        filter_messages,
        redact_preview,
    )
    FILTER_AVAILABLE = True
except ImportError:
    FILTER_AVAILABLE = False

    def is_sensitive_message(**_kwargs):
        return False

    def filter_messages(messages, **_kwargs):
        return messages

    def redact_preview(text, **_kwargs):
        return text
