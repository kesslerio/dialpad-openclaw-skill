# AGENTS.md - Dialpad OpenClaw Skill

## Scope

This is the `dialpad-openclaw-skill` repo. It contains the Dialpad webhook server,
SMS/call enrichment, and OpenClaw hook integration.

## PR Review Policy

- Wait for the Codex automated review before merging. Codex reviews are triggered on PR open / ready-for-review.
- Do not squash-merge until the Codex review has either posted findings (address them first) or reacted with 👍 (no findings).
- If Codex does not review within 10 minutes, proceed without blocking.

## Managed environment & test runner

- `vendor/` is the managed dependency tree for the generated Dialpad CLI — **built, not tracked**: it is constructed from the pinned, hash-verified `requirements.txt` by `scripts/build_vendor.py` during runtime-copy delivery (`docs/reference/runtime-copies.md`) and before test runs; provenance, upgrades, and verification: `docs/reference/vendor-build.md`. `bin/_dialpad_compat.py` and the `generated/dialpad` facade put it on `PYTHONPATH` for every CLI subprocess; do not reintroduce ambient `uv`/`PATH` dependency discovery — the deployed gateway runtime carries no `uv` (#155).
- After a local wrapper failure in `bin/send_sms.py`, a claimed approval draft goes back to a retryable state via `scripts/sms_approval.py:release_agent_direct_send_claim` and is never terminally `failed`; only provider-result failures use `fail_agent_direct_send`/`record_agent_direct_send`.
- Run tests with `uv run --with pytest python -m pytest tests/ -q` (system `python3` here has no pytest), and unset `DIALPAD_WEBHOOK_SECRET` first — an ambient secret makes the unsigned webhook-handler tests fail at baseline.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
