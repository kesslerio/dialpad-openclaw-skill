# Plan: Issue 50 Call History Wrapper

Date: 2026-03-25
Issues: #50
Depth: Lightweight

## Problem Frame

The repository can already retrieve recent calls through operator tooling in `scripts/list_calls.py`, but there is no supported `bin/*` wrapper for agents. That leaves issue #50 unresolved at the actual contract boundary: agents still cannot use a stable call-history command even though the raw capability exists.

## Scope

In scope:

- Add a supported agent-facing wrapper for recent call history
- Reuse the existing call-list implementation rather than rebuilding it through a new API path
- Add JSON-envelope behavior consistent with other wrappers
- Document the new wrapper in top-level and reference docs

Out of scope:

- Reworking the generated CLI `call.list` command
- Replacing or deleting `scripts/list_calls.py`
- Adding transcript, recording download, or AI recap retrieval to the new wrapper
- Changing webhook, voicemail, or storage behavior

## Requirements Trace

### Issue #50

- Agents need a stable command to retrieve recent calls
- The command must expose enough recent-call detail to identify the right call
- The capability should unblock downstream call lookup workflows without requiring direct web-app usage

## Current Evidence

- `scripts/list_calls.py` already fetches and formats recent calls from `https://dialpad.com/api/v2/call`
- `tests/test_list_calls.py` already covers the core call-fetching behavior
- `bin/*` wrappers are the documented supported agent interface in `README.md` and `SKILL.md`
- `_dialpad_compat.py` already defines the shared JSON envelope and error contract used by wrapper commands

## Decisions

1. Add `bin/list_calls.py` as the supported agent-facing entrypoint.
   Rationale: This resolves the issue at the repo's documented contract boundary instead of treating operator tooling as good enough.

2. Reuse logic from `scripts/list_calls.py` instead of routing through the generated CLI.
   Rationale: The raw HTTP path already works and is tested, while the issue explicitly notes the generated path is not reliable for this workflow.

3. Support both human-readable output and `--json` envelope output.
   Rationale: Human operators still benefit from the table view, while agents need a deterministic machine-readable response.

4. Keep this slice focused on recent call listing only.
   Rationale: Pulling in transcript or recording retrieval would expand scope beyond what issue #50 actually asks for.

## Files

- `bin/list_calls.py`
- `bin/_dialpad_compat.py`
- `scripts/list_calls.py`
- `tests/test_json_contract.py`
- `tests/test_list_calls.py`
- `README.md`
- `SKILL.md`
- `references/api-reference.md`
- `references/architecture.md`

## Implementation Units

### Unit 1: Wrapper Surface

Goal:
- Expose recent call history through a supported `bin/*` wrapper

Files:
- `bin/list_calls.py`
- `bin/_dialpad_compat.py`
- `scripts/list_calls.py`

Approach:
- Add a new wrapper with task-focused flags mirroring the existing script behavior
- Reuse the existing fetch/normalize/render helpers from `scripts/list_calls.py` where practical
- Add a wrapper command id to the shared compat module
- Return a success/error JSON envelope when `--json` is requested

Patterns to follow:
- `bin/send_sms.py`
- `bin/export_sms.py`
- `tests/test_json_contract.py`

Verification:
- `bin/list_calls.py --json` returns the standard wrapper envelope
- Validation and runtime failures map into the standard error envelope
- Non-JSON mode still provides readable recent-call output

### Unit 2: Coverage and Docs

Goal:
- Lock the new wrapper into tests and document it as the supported path

Files:
- `tests/test_json_contract.py`
- `tests/test_list_calls.py`
- `README.md`
- `SKILL.md`
- `references/api-reference.md`
- `references/architecture.md`

Approach:
- Extend JSON-contract tests for the new wrapper
- Add focused wrapper tests for dry failure/success cases and output shape
- Update docs so call history appears alongside the other supported `bin/*` commands

Patterns to follow:
- Existing wrapper docs in `README.md`, `SKILL.md`, and `references/api-reference.md`

Verification:
- Docs consistently point agents to `bin/list_calls.py`
- Tests cover wrapper JSON success and validation failure
- Existing list-call behavior coverage remains intact

## Risks

- Duplicating too much logic between the wrapper and the operator script
- Returning a JSON shape that is inconsistent with the repo's existing wrapper contract
- Overexpanding the feature by chasing adjacent call-detail requests from issue text

## Test Scenarios

1. Run the existing recent-call fetch tests to confirm the API path and missed-call filtering still behave correctly.
2. Verify `bin/list_calls.py --json` returns `ok/command/data/meta` on success.
3. Verify invalid arguments in JSON mode return the standard error envelope.
4. Verify docs show `bin/list_calls.py` as the supported agent-facing call history command.

## Verification

- `python3 -m pytest`
- `git diff --check`
- Manual doc sweep for `bin/list_calls.py` references

## Alternative Considered

Fix the generated CLI `call.list` flow and use that as the wrapper backend.

Rejected because the issue's triage note already points to the working script path as the right slice, and changing the generated path would add risk without improving the supported contract.
