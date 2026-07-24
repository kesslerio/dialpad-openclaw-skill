from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
import sys
from unittest.mock import patch

import pytest


FACADE_PATH = Path(__file__).resolve().parent.parent / "generated" / "dialpad"
FACADE_SPEC = importlib.util.spec_from_loader(
    "generated_dialpad_facade_policy_test",
    SourceFileLoader("generated_dialpad_facade_policy_test", str(FACADE_PATH)),
)
assert FACADE_SPEC is not None and FACADE_SPEC.loader is not None
facade = importlib.util.module_from_spec(FACADE_SPEC)
FACADE_SPEC.loader.exec_module(facade)


@pytest.mark.parametrize(
    "argv",
    [
        ["sms", "send", "--to-numbers", '["+442071838750"]'],
        [
            "message",
            "bulk_messages.send",
            "--data",
            '{"to_numbers":["+442071838750"]}',
        ],
        [
            "message",
            "schedules.create",
            "--to-numbers=+442071838750",
        ],
        ["call", "make", "--phone-number", "+442071838750"],
        [
            "call",
            "call.initiate_ivr_call",
            "--data",
            '{"phone_number":"+442071838750"}',
        ],
        ["callback", "call.callback", "--phone-number", "+442071838750"],
        ["users", "users.initiate_call", "--phone-number", "+442071838750"],
        ["call", "call.transfer_call", "--to", "+442071838750"],
        ["call", "call.participants.add", "--participant", "+442071838750"],
        [
            "call",
            "call.transfer_call",
            "--data",
            '{"to":{"number":"+442071838750"}}',
        ],
        [
            "call",
            "call.participants.add",
            "--data",
            '{"participant":{"number":"+442071838750"}}',
        ],
    ],
)
def test_generated_facade_rejects_international_destinations(argv):
    translated = facade._translate(argv)

    with pytest.raises(ValueError, match="NANP"):
        facade._preflight_outbound_destination(translated)


@pytest.mark.parametrize(
    "argv",
    [
        ["sms", "send", "--to-numbers", '["+14155550100"]'],
        [
            "message",
            "bulk_messages.send",
            "--data",
            '{"to_numbers":["+14155550100","+16045550101"]}',
        ],
        ["message", "schedules.create", "--to-numbers=+14155550100"],
        ["call", "make", "--phone-number", "+14155550100"],
        [
            "call",
            "call.initiate_ivr_call",
            "--data",
            '{"phone_number":"+14155550100"}',
        ],
        ["callback", "call.callback", "--phone-number", "+14155550100"],
        ["users", "users.initiate_call", "--phone-number", "+14155550100"],
        ["call", "call.transfer_call", "--to", "+14155550100"],
        ["call", "call.participants.add", "--participant", "+14155550100"],
    ],
)
def test_generated_facade_allows_nanp_destinations(argv):
    facade._preflight_outbound_destination(facade._translate(argv))


def test_generated_facade_disables_unverifiable_scheduled_send():
    with pytest.raises(ValueError, match="cannot validate"):
        facade._preflight_outbound_destination(
            ["message", "schedules.send_now", "--id", "schedule-123"]
        )


def test_generated_facade_rejects_schedule_update_without_recipients():
    with pytest.raises(ValueError, match="stored recipients"):
        facade._preflight_outbound_destination(
            [
                "message",
                "schedules.update",
                "--id",
                "schedule-123",
                "--start-date",
                "1785000000",
            ]
        )


def test_generated_facade_rejects_international_meeting_callout():
    with pytest.raises(ValueError, match="NANP"):
        facade._preflight_outbound_destination(
            [
                "meetings",
                "meetings.create",
                "--data",
                json.dumps(
                    {
                        "call_out": True,
                        "participants_info": [
                            {"phone_number": "+442071838750"},
                            {"email": "internal@example.com"},
                        ],
                    }
                ),
            ]
        )


def test_generated_facade_allows_meeting_create_without_callout():
    facade._preflight_outbound_destination(
        [
            "meetings",
            "meetings.create",
            "--title",
            "Internal meeting",
        ]
    )


def test_generated_facade_rejects_unverifiable_meeting_update():
    with pytest.raises(ValueError, match="stored call-out state"):
        facade._preflight_outbound_destination(
            [
                "meetings",
                "meetings.update",
                "--title",
                "Rescheduled",
            ]
        )

    with pytest.raises(ValueError, match="Enabling meeting call-out"):
        facade._preflight_outbound_destination(
            [
                "meetings",
                "meetings.update",
                "--call-out",
                "true",
                "--title",
                "Rescheduled",
            ]
        )


def test_generated_facade_allows_meeting_update_that_disables_callout():
    facade._preflight_outbound_destination(
        [
            "meetings",
            "meetings.update",
            "--call-out",
            "false",
            "--title",
            "Rescheduled",
        ]
    )


@pytest.mark.parametrize("command", [["sms", "sms.send"], ["call", "call.call"]])
def test_generated_facade_requires_explicit_nanp_even_with_country_inference(command):
    with pytest.raises(ValueError, match="NANP"):
        facade._preflight_outbound_destination(
            [
                *command,
                "--data",
                (
                    '{"to_numbers":["4155550100"],"infer_country_code":true}'
                    if command[0] == "sms"
                    else '{"phone_number":"4155550100","infer_country_code":true}'
                ),
            ]
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["sms", "sms.send", "--channel-hashtag", "sales"],
        ["message", "schedules.create", "--channel-hashtag", "sales"],
    ],
)
def test_generated_facade_allows_explicit_channel_destination(argv):
    facade._preflight_outbound_destination(argv)


@pytest.mark.parametrize("channel_hashtag", ["", "sales", " sales "])
def test_generated_facade_rejects_channel_only_schedule_update(
    channel_hashtag,
):
    with pytest.raises(ValueError, match="stored recipients"):
        facade._preflight_outbound_destination(
            [
                "message",
                "schedules.update",
                "--id",
                "schedule-123",
                f"--channel-hashtag={channel_hashtag}",
            ]
        )


def test_generated_facade_allows_non_phone_transfer_target():
    facade._preflight_outbound_destination(
        ["call", "call.transfer_call", "--to", "user-id-123"]
    )


def test_generated_facade_allows_non_phone_participant_target():
    facade._preflight_outbound_destination(
        ["call", "call.participants.add", "--participant", "user-id-123"]
    )


@pytest.mark.parametrize("call_out", ["true", "t", "yes", "y", "on", "1"])
def test_generated_facade_rejects_all_click_true_meeting_update_values(call_out):
    with pytest.raises(ValueError, match="Enabling meeting call-out"):
        facade._preflight_outbound_destination(
            [
                "meetings",
                "meetings.update",
                "--call-out",
                call_out,
                "--title",
                "Rescheduled",
            ]
        )


def test_generated_facade_rejects_invalid_meeting_boolean():
    with pytest.raises(ValueError, match="Invalid boolean"):
        facade._preflight_outbound_destination(
            [
                "meetings",
                "meetings.update",
                "--call-out",
                "maybe",
                "--title",
                "Rescheduled",
            ]
        )


@pytest.mark.parametrize(
    "argv",
    [
        [
            "call",
            "call.transfer_call",
            "--data",
            '{"to":{"call_id":123}}',
        ],
        [
            "call",
            "call.transfer_call",
            "--data",
            '{"to":{"target_id":123,"target_type":"user"}}',
        ],
        [
            "call",
            "call.participants.add",
            "--data",
            '{"participant":{"target_id":123,"target_type":"user"}}',
        ],
    ],
)
def test_generated_facade_allows_structured_internal_targets(argv):
    facade._preflight_outbound_destination(argv)


def test_generated_facade_rejects_unknown_structured_destination():
    with pytest.raises(ValueError, match="supported variant"):
        facade._preflight_outbound_destination(
            [
                "call",
                "call.transfer_call",
                "--data",
                '{"to":{"unexpected":"+442071838750"}}',
            ]
        )


@pytest.mark.parametrize("field", ["to", "participant"])
def test_generated_facade_rejects_non_e164_structured_phone(field):
    command = (
        ["call", "call.transfer_call"]
        if field == "to"
        else ["call", "call.participants.add"]
    )
    with pytest.raises(ValueError, match="NANP"):
        facade._preflight_outbound_destination(
            [
                *command,
                "--data",
                json.dumps({field: {"number": "442071838750"}}),
            ]
        )


def test_generated_facade_uses_last_duplicate_option_like_click():
    with pytest.raises(ValueError, match="NANP"):
        facade._preflight_outbound_destination(
            [
                "sms",
                "sms.send",
                "--data",
                '{"to_numbers":["+14155550100"]}',
                "--data",
                '{"to_numbers":["+442071838750"]}',
            ]
        )


def test_generated_facade_rejects_whitespace_it_would_send_unchanged():
    with pytest.raises(ValueError, match="NANP"):
        facade._preflight_outbound_destination(
            [
                "sms",
                "sms.send",
                "--to-numbers",
                " +14155550100 ",
            ]
        )


def test_generated_facade_main_rejects_before_raw_cli_subprocess():
    with patch.object(
        sys,
        "argv",
        [
            "generated/dialpad",
            "sms",
            "send",
            "--to-numbers",
            '["+442071838750"]',
        ],
    ), patch.object(facade, "_raw_cli_runner") as runner, patch.object(
        facade.subprocess, "run"
    ) as run:
        assert facade.main() == 2

    runner.assert_not_called()
    run.assert_not_called()


def test_generated_facade_subcommand_help_bypasses_request_preflight():
    with patch.object(
        sys,
        "argv",
        [
            "generated/dialpad",
            "message",
            "schedules.update",
            "--help",
        ],
    ), patch.object(
        facade,
        "_raw_cli_runner",
        return_value=["python3"],
    ), patch.object(
        facade.subprocess,
        "run",
        return_value=type("Completed", (), {"returncode": 0})(),
    ) as run:
        assert facade.main() == 0

    run.assert_called_once()


def test_generated_facade_does_not_mistake_option_value_for_help():
    with patch.object(
        sys,
        "argv",
        [
            "generated/dialpad",
            "sms",
            "send",
            "--to-numbers",
            '["+442071838750"]',
            "--text",
            "--help",
        ],
    ), patch.object(facade, "_raw_cli_runner") as runner, patch.object(
        facade.subprocess,
        "run",
    ) as run:
        assert facade.main() == 2

    runner.assert_not_called()
    run.assert_not_called()


def test_generated_facade_recognizes_eager_help_before_other_options():
    with patch.object(
        sys,
        "argv",
        [
            "generated/dialpad",
            "call",
            "make",
            "--help",
            "--phone-number",
            "+442071838750",
        ],
    ), patch.object(
        facade,
        "_raw_cli_runner",
        return_value=["python3"],
    ), patch.object(
        facade.subprocess,
        "run",
        return_value=type("Completed", (), {"returncode": 0})(),
    ) as run:
        assert facade.main() == 0

    run.assert_called_once()
