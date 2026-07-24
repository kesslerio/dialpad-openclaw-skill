"""Fail-closed destination checks for the generated Dialpad CLI facade."""

from __future__ import annotations

import json
from dataclasses import dataclass

from outbound_destination_policy import require_supported_outbound_destinations


Command = tuple[str, str]


@dataclass(frozen=True)
class DestinationRule:
    field: str
    allows_internal_target: bool = False
    requires_explicit_destination: bool = False
    alternate_internal_field: str | None = None
    internal_variants: tuple[frozenset[str], ...] = ()


@dataclass(frozen=True)
class DestinationSelection:
    phone_numbers: list[str]
    is_internal: bool


DESTINATION_RULES: dict[Command, DestinationRule] = {
    ("sms", "sms.send"): DestinationRule(
        "to_numbers",
        alternate_internal_field="channel_hashtag",
    ),
    ("message", "bulk_messages.send"): DestinationRule("to_numbers"),
    ("message", "schedules.create"): DestinationRule(
        "to_numbers",
        alternate_internal_field="channel_hashtag",
    ),
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
        internal_variants=(
            frozenset({"call_id"}),
            frozenset({"operator_id", "target_id"}),
            frozenset({"target_id", "target_type"}),
        ),
    ),
    ("call", "call.participants.add"): DestinationRule(
        "participant",
        allows_internal_target=True,
        internal_variants=(frozenset({"target_id", "target_type"}),),
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


def subcommand_help_requested(argv: list[str]) -> bool:
    """Return whether Click will treat a help token as an eager option."""

    command_start = _command_start(argv)
    skip_value = False
    for arg in argv[command_start + 2 :]:
        if skip_value:
            skip_value = False
            continue
        if arg in {"-h", "--help"}:
            return True
        if arg.startswith("-") and "=" not in arg:
            skip_value = True
    return False


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


def _structured_destination_numbers(
    value: object,
    rule: DestinationRule,
) -> DestinationSelection | None:
    if not rule.allows_internal_target:
        return None
    if isinstance(value, str) and value.lstrip().startswith("{"):
        value = json.loads(value.lstrip())
    if not isinstance(value, dict):
        return None
    if "number" in value:
        return DestinationSelection([str(value["number"])], is_internal=False)
    keys = frozenset(value)
    if any(required_keys <= keys for required_keys in rule.internal_variants):
        return DestinationSelection([], is_internal=True)
    raise ValueError("Structured call destination has no supported variant")


def _parse_click_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _body_from_args(command_args: list[str]) -> tuple[dict[str, object], bool]:
    data_value = _option_value(command_args, "--data")
    if data_value is None:
        return {}, False
    # generated/dialpad.openapi replaces its option-built body with --data
    # wholesale, so preflight must use the same precedence rather than merge.
    decoded = json.loads(data_value)
    if not isinstance(decoded, dict):
        raise ValueError("--data must be a JSON object")
    return decoded, True


def _effective_value(
    field: str,
    command_args: list[str],
    body: dict[str, object],
    data_supplied: bool,
) -> object | None:
    if data_supplied:
        return body.get(field)
    return _option_value(command_args, f"--{field.replace('_', '-')}")


def _preflight_meeting(
    command: Command,
    command_args: list[str],
    body: dict[str, object],
    data_supplied: bool,
) -> None:
    call_out_value = _effective_value(
        "call_out",
        command_args,
        body,
        data_supplied,
    )
    if command == ("meetings", "meetings.update") and call_out_value is None:
        raise ValueError(
            "Meeting updates must explicitly set call_out because stored "
            "call-out state and participants cannot be validated locally."
        )
    if call_out_value is None:
        return
    if _parse_click_bool(call_out_value):
        raise ValueError(
            "Meeting call-out is disabled because its effective participants "
            "cannot be validated locally."
        )


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

    body, data_supplied = _body_from_args(command_args)
    if command in {("meetings", "meetings.create"), ("meetings", "meetings.update")}:
        _preflight_meeting(command, command_args, body, data_supplied)
        return

    rule = DESTINATION_RULES.get(command)
    if rule is None:
        return

    recipient_value = _effective_value(
        rule.field,
        command_args,
        body,
        data_supplied,
    )
    alternate_value = None
    if rule.alternate_internal_field is not None:
        alternate_value = _effective_value(
            rule.alternate_internal_field,
            command_args,
            body,
            data_supplied,
        )
    has_alternate_destination = (
        isinstance(alternate_value, str)
        and bool(alternate_value)
        and alternate_value == alternate_value.strip()
    )
    if recipient_value is None and has_alternate_destination:
        return
    if recipient_value is None and rule.requires_explicit_destination:
        raise ValueError(
            "Schedule updates must include explicit phone or channel recipients "
            "because stored recipients cannot be validated locally."
        )
    if recipient_value is None:
        return
    structured_numbers = _structured_destination_numbers(recipient_value, rule)
    if structured_numbers is not None:
        recipients = structured_numbers.phone_numbers
    elif rule.field == "to_numbers":
        recipients = _recipient_list(recipient_value)
    else:
        recipients = [str(recipient_value)] if recipient_value is not None else []
    if (
        rule.requires_explicit_destination
        and not recipients
        and not has_alternate_destination
    ):
        raise ValueError(
            "Schedule updates must include explicit phone or channel recipients "
            "because stored recipients cannot be validated locally."
        )
    if (
        rule.allows_internal_target
        and structured_numbers is None
        and recipients
        and not recipients[0].startswith("+")
    ):
        raise ValueError(
            "Internal call destinations must use a supported structured object"
        )
    if recipients:
        require_supported_outbound_destinations(recipients)
