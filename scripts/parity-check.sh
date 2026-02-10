#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -x generated/dialpad ]]; then
  echo "FAIL generated/dialpad is missing or not executable"
  exit 1
fi

declare -a legacy_scripts=(
  "send_sms.py"
  "make_call.py"
  "lookup_contact.py"
  "export_sms.py"
  "create_sms_webhook.py"
)

declare -A wrapper_map=(
  [send_sms.py]="bin/send_sms.py"
  [make_call.py]="bin/make_call.py"
  [lookup_contact.py]="bin/lookup_contact.py"
  [export_sms.py]="bin/export_sms.py"
  [create_sms_webhook.py]="bin/create_sms_webhook.py"
)

declare -A new_command_map=(
  [send_sms.py]="sms send"
  [make_call.py]="call make"
  [lookup_contact.py]="contact lookup"
  [export_sms.py]="sms export"
  [create_sms_webhook.py]="webhook create"
)

failures=0

printf "%-26s %-8s %-8s %-8s\n" "SCRIPT" "LEGACY" "WRAPPER" "NEW_CMD"
printf "%-26s %-8s %-8s %-8s\n" "--------------------------" "------" "-------" "-------"

for script in "${legacy_scripts[@]}"; do
  legacy_ok="ok"
  wrapper_ok="ok"
  cmd_ok="ok"

  [[ -f "$script" ]] || legacy_ok="missing"

  wrapper="${wrapper_map[$script]}"
  if [[ ! -x "$wrapper" ]]; then
    wrapper_ok="missing"
  fi

  read -r cmd_a cmd_b <<< "${new_command_map[$script]}"
  if ! generated/dialpad "$cmd_a" "$cmd_b" --help >/dev/null 2>&1; then
    cmd_ok="missing"
  fi

  if [[ "$legacy_ok" != "ok" || "$wrapper_ok" != "ok" || "$cmd_ok" != "ok" ]]; then
    failures=$((failures + 1))
  fi

  printf "%-26s %-8s %-8s %-8s\n" "$script" "$legacy_ok" "$wrapper_ok" "$cmd_ok"
done

if [[ $failures -gt 0 ]]; then
  echo
  echo "Parity check failed: $failures mapping(s) incomplete"
  exit 1
fi

echo

echo "Parity check passed: legacy scripts, wrappers, and mapped commands are present"
