"""Fail-closed destination checks for the generated Dialpad CLI facade."""

from __future__ import annotations

import json
from dataclasses import dataclass

from outbound_destination_policy import require_supported_outbound_destinations


Command = tuple[str, str]


@dataclass(frozen=True)
class DestinationRule:
    field: str
    supports_country_inference: bool = False
    allows_internal_target: bool = False
    requires_explicit_destination: bool = False


DESTINATION_RULES: dict[Command, DestinationRule] = {
    ("sms", "sms.send"): DestinationRule(
        "to_numbers",
        supports_country_inference=True,
    ),
    ("message", "bulk_messages.send"): DestinationRule("to_numbers"),
    ("message", "schedules.create"): DestinationRule("to_numbers"),
    ("message", "schedules.update"): DestinationRule(
        "to_numbers",
        requires_explicit_destination=True,
    ),
    ("call", "call.call"): DestinationRule("phone_number"),
    ("call", "call.initiate_ivr_call"): DestinationRule("phone_number"),
    ("callback", "call.callback"): DestinationRule("phone_number"),
    ("users", "users.initiate_call"): DestinationRule("phone_number"),
    ("call", "call.transfer_call"): DestinationRule(
        "to",
        allows_internal_target=True,
    ),
    ("call", "call.participants.add"): DestinationRule(
        "participant",
        allows_internal_target=True,
    ),
}


def _command_start(argv: list[str]) -> int:
    """Return the index of the first command, skipping global options."""

    value_options = {"--output", "-o", "--base-url", "-b", "--token", "--api-key"}
    skip_next = False
    for index, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if arg in value_options:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return index
    return len(argv)


def _option_value(argv: list[str], option: str) -> str | None:
    for index in range(len(argv) - 1, -1, -1):
        arg = argv[index]
        if arg == option:
            return argv[index + 1] if index + 1 < len(argv) else None
        prefix = f"{option}="
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return None


def _recipient_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not isinstance(value, str):
        raise ValueError(
            "Outbound recipients must be a phone number or list of phone numbers"
        )
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return [item for item in value.split(",") if item]
    if isinstance(decoded, list):
        return [str(item) for item in decoded]
    if isinstance(decoded, str):
        return [decoded]
    raise ValueError(
        "Outbound recipients must be a phone number or list of phone numbers"
    )


def _is_true(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _body_from_args(command_args: list[str]) -> dict[str, object]:
    data_value = _option_value(command_args, "--data")
    if data_value is None:
        return {}
    decoded = json.loads(data_value)
    if not isinstance(decoded, dict):
        raise ValueError("--data must be a JSON object")
    return decoded


def _preflight_meeting(
    command: Command,
    command_args: list[str],
    body: dict[str, object],
) -> None:
    call_out_value = body.get(
        "call_out",
        _option_value(command_args, "--call-out"),
    )
    participants_value = body.get(
        "participants_info",
        _option_value(command_args, "--participants-info"),
    )
    if command == ("meetings", "meetings.update") and call_out_value is None:
        raise ValueError(
            "Meeting updates must explicitly set call_out because stored "
            "call-out state and participants cannot be validated locally."
        )
    if not _is_true(call_out_value):
        return
    if command == ("meetings", "meetings.update") and participants_value is None:
        raise ValueError(
            "Call-out meeting updates must include explicit participants "
            "because stored participants cannot be validated locally."
        )
    if participants_value is None:
        return
    if isinstance(participants_value, str):
        participants_value = json.loads(participants_value)
    if not isinstance(participants_value, list) or any(
        not isinstance(participant, dict) for participant in participants_value
    ):
        raise ValueError("Meeting participants must be a JSON list of objects")
    phone_numbers = [
        str(participant[field])
        for participant in participants_value
        for field in ("phone", "phone_number")
        if participant.get(field) is not None
    ]
    if phone_numbers:
        require_supported_outbound_destinations(phone_numbers)


def preflight_outbound_destination(argv: list[str]) -> None:
    """Reject unsupported outbound destinations before invoking generated code."""

    command_start = _command_start(argv)
    if len(argv) < command_start + 2:
        return
    command = (argv[command_start], argv[command_start + 1])
    command_args = argv[command_start + 2 :]

    if command == ("message", "schedules.send_now"):
        raise ValueError(
            "The generated schedule send command is disabled because it cannot "
            "validate stored recipients; use a validated SMS wrapper instead."
        )

    body = _body_from_args(command_args)
    if command in {("meetings", "meetings.create"), ("meetings", "meetings.update")}:
        _preflight_meeting(command, command_args, body)
        return

    rule = DESTINATION_RULES.get(command)
    if rule is None:
        return

    option_name = f"--{rule.field.replace('_', '-')}"
    recipient_value = body.get(
        rule.field,
        _option_value(command_args, option_name),
    )
    if rule.requires_explicit_destination and recipient_value is None:
        raise ValueError(
            "Schedule updates must include explicit recipients because stored "
            "recipients cannot be validated locally."
        )
    recipients = (
        _recipient_list(recipient_value)
        if rule.field == "to_numbers"
        else ([str(recipient_value)] if recipient_value is not None else [])
    )
    if rule.requires_explicit_destination and not recipients:
        raise ValueError(
            "Schedule updates must include explicit recipients because stored "
            "recipients cannot be validated locally."
        )
    if (
        rule.allows_internal_target
        and recipients
        and not recipients[0].startswith("+")
    ):
        return
    if recipients:
        infer_country_code = body.get(
            "infer_country_code",
            _option_value(command_args, "--infer-country-code"),
        )
        require_supported_outbound_destinations(
            recipients,
            allow_nanp_national=(
                rule.supports_country_inference
                and _is_true(infer_country_code)
            ),
        )
