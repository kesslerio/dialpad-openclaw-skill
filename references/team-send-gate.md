# Team Send Gate (Phase 1)

Shared team assistants may send SMS only through the gated path enforced by
`scripts/send_gate.py`, wired into `bin/send_sms.py`, `scripts/send_sms.py`,
and `bin/send_group_intro.py`. No provider call happens before the gate
passes, including the approval-draft resolve path and the generated-CLI path.

## What the gate enforces (all must pass, fail closed)

- **Caller allowlist.** `--caller` (or `DIALPAD_SEND_CALLER`) must name a caller
  in the config. A caller may send from the shared lines it is granted plus,
  when `acts_for` names a teammate, that teammate's own Dialpad line — never
  another teammate's. `extra_allowed_lines` extends the set (operator caller).
  The from-number must match an approved line exactly as E.164 (a bare
  10-digit or 1+10-digit NANP national form reads as `+1...`).
- **Opt-out union.** Dialpad company list (`GET /api/v2/company/{id}/smsoptout?opt_out_state=opted_out`,
  full cursor pagination, company from `DIALPAD_COMPANY_ID`, key from
  `DIALPAD_API_KEY`) UNION `sms_approval_opt_outs` + emergency opt-out markers.
  Either source unreadable refuses the send.
- **Recipient-local window:** 08:00 <= local < 21:00 in every zone the area
  code can cover (`scripts/nanp_area_code_timezones.py`). Unknown or
  non-US/Canada area codes refuse.
- **Minute rule:** recipient-local minute must not be :00/:15/:30/:45.
  Retryable refusals carry `retry_after_seconds`.
- Group sends refuse if any recipient fails.

Refusals exit non-zero with `send_gate_refused` in the wrapper JSON envelope;
allowlist/opt-out refusals are non-retryable, window/minute retryable.
Confirmed sends are recorded via `record_outbound_observation` as before; the
canonical message schema cannot carry caller identity, so caller (and
`acting_for`) ride on the receipt-ledger line instead — no schema change.

## Config

Path from `DIALPAD_SEND_GATE_CONFIG`; missing/unreadable config refuses every
send. Shape (see `references/send-gate.example.json`):

```json
{
  "shared_lines": {"office": "+14159065785", "sales": "+14155201316"},
  "teammates": {"scott": "+14152230323"},
  "callers": {
    "team-scott": {"shared_lines": ["office", "sales"], "acts_for": "scott"},
    "operator": {"shared_lines": ["office", "sales"],
                 "extra_allowed_lines": ["+1XXXXXXXXXX"]}
  }
}
```

`time_restricted` (default `true`) controls the window + minute checks for
that caller; setting it `false` for a caller is an owner decision. The
example's `"+1XXXXXXXXXX"` is a placeholder, not a line: replace
`extra_allowed_lines` with a real line the owner approves for the operator
caller or remove the key. On the host the file lives outside the repo next to
the other Dialpad secrets, referenced from the environment alongside
`DIALPAD_COMPANY_ID`.

## Paths

- **Gated:** `bin/send_sms.py`, `scripts/send_sms.py` (its `main()`),
  `bin/send_group_intro.py` (both mirrored recipients must pass).
- **Not gated:** the human operator approval paths — the Telegram approval
  callback in `scripts/webhook_server.py` and `scripts/sms_approval.py`'s
  approve flow call `scripts/send_sms.send_sms()` directly after a person
  approves — and the raw `generated/dialpad` CLI.
- Phase 2 keeps shared team assistants off the raw API key, so the gated
  endpoint is their only send path; direct key holders stay on the gated
  CLIs.

## Dry run

```bash
bin/send_sms.py --to "+14155551234" --from "+14159065785" --caller team-scott \
  --message "hi" --dry-run --json
```

Evaluates the full gate (both opt-out sources read) and prints the decision
plus what would be sent; no `POST /sms`, no shared-log write. Non-zero exit on
refusal.

## Phase 2 (not built)

A small HTTP endpoint on the deploy host, bound to 127.0.0.1 and exposed only
through a token-run cloudflared tunnel hostname behind Cloudflare Access (the
team knowledge brain pattern). One Access service token per assistant plus an
app-level bearer per assistant, each in 1Password and revocable alone; the
origin verifies the Access JWT (`Cf-Access-Jwt-Assertion`, AUD check) and maps
the token to a gate caller id. Shared-log reads go through the existing
Interaction Log API with a read-scoped token.

## Owner actions

- [ ] Approve the caller-to-line mapping, including whether a teammate's
      assistant may send only from its own person's line.
- [ ] Create the config on the host (`DIALPAD_SEND_GATE_CONFIG`); approve or
      remove the operator caller's `extra_allowed_lines` placeholder and its
      `time_restricted` setting.
- [ ] Set `DIALPAD_COMPANY_ID`.
- [ ] Confirm the `DIALPAD_API_KEY` on the host is a company-admin key — the
      smsoptout endpoint requires one — and that the approval DB with
      `sms_approval_opt_outs` exists; otherwise every send is refused.
- [ ] Decide whether operator-approved sends (Telegram approval / draft
      approve) should also pass the gate.
- [ ] Phase 2: create the Access app + service tokens, the tunnel route, and
      the app tokens.
