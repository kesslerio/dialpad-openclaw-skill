from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
POLICY_SPEC = importlib.util.spec_from_file_location(
    "outbound_destination_policy",
    SCRIPTS_DIR / "outbound_destination_policy.py",
)
assert POLICY_SPEC is not None and POLICY_SPEC.loader is not None
outbound_destination_policy = importlib.util.module_from_spec(POLICY_SPEC)
sys.modules["outbound_destination_policy"] = outbound_destination_policy
POLICY_SPEC.loader.exec_module(outbound_destination_policy)

SEND_SMS_SPEC = importlib.util.spec_from_file_location(
    "legacy_send_sms_policy_test",
    SCRIPTS_DIR / "send_sms.py",
)
assert SEND_SMS_SPEC is not None and SEND_SMS_SPEC.loader is not None
legacy_send_sms = importlib.util.module_from_spec(SEND_SMS_SPEC)
SEND_SMS_SPEC.loader.exec_module(legacy_send_sms)

MAKE_CALL_SPEC = importlib.util.spec_from_file_location(
    "legacy_make_call_policy_test",
    SCRIPTS_DIR / "make_call.py",
)
assert MAKE_CALL_SPEC is not None and MAKE_CALL_SPEC.loader is not None
legacy_make_call = importlib.util.module_from_spec(MAKE_CALL_SPEC)
MAKE_CALL_SPEC.loader.exec_module(legacy_make_call)


def test_approval_lane_transport_rejects_non_nanp_recipient_before_api(monkeypatch):
    monkeypatch.setattr(legacy_send_sms, "DIALPAD_API_KEY", "test-key")

    with patch.object(legacy_send_sms.urllib.request, "urlopen") as urlopen:
        with pytest.raises(ValueError, match="NANP"):
            legacy_send_sms.send_sms(
                ["+442071838750"],
                "Hello",
                from_number="+14155201316",
            )

    urlopen.assert_not_called()


def test_approval_lane_transport_rejects_mixed_batch_before_api(monkeypatch):
    monkeypatch.setattr(legacy_send_sms, "DIALPAD_API_KEY", "test-key")

    with patch.object(legacy_send_sms.urllib.request, "urlopen") as urlopen:
        with pytest.raises(ValueError, match="NANP"):
            legacy_send_sms.send_sms(
                ["+14155550100", "+442071838750"],
                "Hello",
                from_number="+14155201316",
            )

    urlopen.assert_not_called()


def test_operator_call_transport_rejects_non_nanp_recipient_before_api(monkeypatch):
    monkeypatch.setattr(legacy_make_call, "DIALPAD_API_KEY", "test-key")

    with patch.object(legacy_make_call.urllib.request, "urlopen") as urlopen:
        with pytest.raises(ValueError, match="NANP"):
            legacy_make_call.make_call(
                "+442071838750",
                user_id="test-user",
            )

    urlopen.assert_not_called()


@pytest.mark.parametrize(
    "phone_number",
    [
        "+1١٢٣٤٥٦٧٨٩٠",
        "+1１２３４５６７８９０",
    ],
)
def test_policy_rejects_unicode_digits(phone_number):
    with pytest.raises(ValueError, match="NANP"):
        outbound_destination_policy.require_supported_outbound_destinations(
            [phone_number]
        )


def test_policy_allows_nanp_national_only_when_country_inference_is_explicit():
    with pytest.raises(ValueError, match="NANP"):
        outbound_destination_policy.require_supported_outbound_destinations(
            ["4155550100"]
        )

    outbound_destination_policy.require_supported_outbound_destinations(
        ["4155550100"],
        allow_nanp_national=True,
    )


def test_policy_rejects_whitespace_instead_of_validating_a_changed_value():
    with pytest.raises(ValueError, match="NANP"):
        outbound_destination_policy.require_supported_outbound_destinations(
            [" +14155550100 "]
        )


def test_policy_explicitly_allows_non_us_nanp_territory():
    outbound_destination_policy.require_supported_outbound_destinations(
        ["+18095550123"]
    )
