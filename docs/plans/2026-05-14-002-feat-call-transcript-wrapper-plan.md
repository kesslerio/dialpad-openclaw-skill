---
date: 2026-05-14
type: feat
status: active
origin: docs/brainstorms/2026-05-14-call-transcript-wrapper-requirements.md
issue: 83
---

# Plan: Call Transcript Wrapper

## Problem Frame

`bin/list_calls.py` is the supported agent-facing command for call history, but it stops at call metadata. Agents that need to review what was said on a call currently have to leave the supported `bin/` wrapper surface and use operator-only scripts, generated CLI internals, or raw API calls.

The requirements doc defines this as a skill contract gap rather than an agent knowledge issue. The implementation should promote transcript retrieval into the same supported wrapper layer as call history, while keeping v1 transcript-only.

---

## Scope

In scope:

- Add a supported agent-facing command for retrieving one Dialpad call transcript.
- Support known `call_id` lookup as the primary flow.
- Support recent-call selection when matching is unambiguous enough for existing call lookup behavior.
- Return standard JSON-envelope output for downstream agents.
- Return concise human-readable output for operator use.
- Gracefully distinguish unavailable transcripts from API/configuration failures.
- Document the transcript workflow next to call history.

Out of scope:

- AI recap retrieval.
- Suggested follow-up replies or sales coaching.
- CRM enrichment, contact sync, or call follow-up automation.
- Bulk transcript export, search, or indexing.
- Reworking call-history listing beyond discoverability needed for transcript retrieval.

### Deferred to Follow-Up Work

- A richer call-context workflow that combines transcript, recap, CRM context, and suggested follow-up.
- Bulk transcript sync/search across historical calls.

---

## Requirements Trace

- R1, F1, AE1: Provide a supported agent-facing transcript retrieval command.
- R2, AE1: Accept a known call id as the primary selection mode.
- R3, AE2: Support recent-call selection only where the existing lookup behavior can resolve a clear target.
- R4, F2, AE3: Make unavailable transcripts explicit rather than presenting empty content as success.
- R5, AE1, AE2: Include transcript text and enough call metadata to verify the selected call.
- R6, AE3: Keep human output concise and distinguish unavailable transcripts from failures.
- R7, AE4: Keep v1 transcript-only.
- R8: Update supported docs so agents can discover the transcript path.

---

## Current Evidence

- `scripts/get_transcript.py` already retrieves transcript data through operator-only tooling and formats several transcript response shapes.
- `scripts/call_lookup.py` already provides shared API and recent-call selection helpers used by transcript/recap operator scripts.
- `scripts/get_ai_recap.py` shows a parallel operator-only call-detail utility that should remain out of v1 scope.
- `bin/list_calls.py` shows the current wrapper pattern for call-history commands, including argument validation, `DIALPAD_API_KEY` enforcement, and JSON envelope output.
- `_dialpad_compat.py` provides command IDs, wrapper errors, JSON success/error envelopes, and shared auth handling.
- `tests/test_json_contract.py`, `tests/test_list_calls_wrapper.py`, `tests/test_call_lookup.py`, and related docs provide direct patterns to extend.

---

## Key Technical Decisions

1. Add a new `bin/` transcript wrapper instead of documenting `scripts/get_transcript.py`.
   Rationale: The product gap is at the supported agent contract boundary, and repo guidance intentionally keeps agents out of `scripts/` for normal work.

2. Reuse existing call lookup and transcript formatting logic where practical.
   Rationale: The operator script already handles several plausible transcript payload shapes and avoids rebuilding selection behavior.

3. Prefer explicit unavailable states over hard failures for "no transcript" conditions.
   Rationale: Missing or still-processing transcripts are expected Dialpad states; agents need to know they cannot use a transcript, not treat the command as broken.

4. Keep the command transcript-only.
   Rationale: The requirements intentionally exclude AI recap, CRM enrichment, and follow-up drafting.

---

## Implementation Units

### U1. Shared Transcript Retrieval Behavior

**Goal:** Make transcript fetching and unavailable-state handling reusable by the new wrapper without broadening the operator script's product scope.

**Requirements:** R1, R4, R5, R6, F1, F2, AE1, AE3.

**Dependencies:** None.

**Files:**

- `scripts/get_transcript.py`
- `scripts/call_lookup.py`
- `tests/test_get_transcript.py`

**Approach:** Preserve the existing operator command while extracting or tightening helper behavior so callers can request a transcript by resolved call id and receive a structured result. Treat transcript not found, absent text, or unsupported response shapes as explicit unavailable outcomes. If implementation reveals Dialpad exposes transcript text more reliably on a call-detail object than on the transcript endpoint, add a narrow fallback inside the shared retrieval behavior while keeping the caller-facing result shape stable.

**Patterns to follow:** `scripts/get_transcript.py` formatting helpers; `scripts/get_ai_recap.py` error handling; `scripts/call_lookup.py` API helper style.

**Test scenarios:**

- Transcript endpoint returns string transcript content; helper returns available transcript text for the requested call id.
- Transcript endpoint returns utterance/segment-style content; helper formats readable text without dropping speaker labels where present.
- Transcript endpoint returns no readable text; helper marks transcript unavailable rather than returning successful empty content.
- Transcript endpoint returns not found; helper marks transcript unavailable in a way the wrapper can map to graceful output.
- API/configuration failures remain distinguishable from unavailable transcript data.

**Verification:** Helper tests cover available, unavailable, and failure paths without requiring live Dialpad credentials.

### U2. Agent-Facing Wrapper

**Goal:** Add the supported `bin/` command that agents can use for transcript retrieval.

**Requirements:** R1, R2, R3, R4, R5, R6, R7, F1, F2, AE1, AE2, AE3, AE4.

**Dependencies:** U1.

**Files:**

- `bin/get_call_transcript.py`
- `bin/_dialpad_compat.py`
- `tests/test_json_contract.py`
- `tests/test_get_call_transcript_wrapper.py`

**Approach:** Add a wrapper with `--call-id` as the primary path and a recent-call selection mode that delegates to existing lookup behavior. Use the standard wrapper parser/error/envelope contract. JSON output should include transcript availability, call id, transcript text when available, and confirming call metadata where the shared helpers can provide it. Human output should print the transcript when available and a concise unavailable message when not. Do not expose recap, suggested replies, CRM enrichment, or follow-up text.

**Patterns to follow:** `bin/list_calls.py` for wrapper structure and JSON filters; `bin/list_sms_thread.py` for local wrapper summary output; `_dialpad_compat.py` command ID conventions.

**Test scenarios:**

- Covers AE1. Given `--call-id call-123 --json` and an available transcript, wrapper emits the standard success envelope with transcript text and call id.
- Covers AE2. Given recent-call selection resolves one call, wrapper emits transcript output for the resolved call id.
- Covers AE3. Given transcript is unavailable, wrapper emits a success envelope with an explicit unavailable status and no fake transcript text.
- Invalid arguments in JSON mode return the standard error envelope.
- API/configuration failures in JSON mode return the standard error envelope, distinct from unavailable transcript data.
- Covers AE4. Wrapper output contains no recap, CRM, suggested reply, or coaching fields.

**Verification:** Wrapper contract tests pass and the command can be exercised in dry/mocked paths without live API credentials.

### U3. Documentation and Discoverability

**Goal:** Make the transcript path discoverable wherever agents learn call workflows.

**Requirements:** R8 plus success criteria from the origin document.

**Dependencies:** U2.

**Files:**

- `SKILL.md`
- `README.md`
- `references/api-reference.md`
- `references/architecture.md`
- `tests/test_openclaw_integration_docs.py`

**Approach:** Add a concise transcript example next to existing call-history examples and clarify that transcript retrieval is the supported path for reading call content. Keep docs explicit that recap, follow-up drafting, and CRM enrichment are out of scope for this wrapper. Update architecture references so `bin/` remains the supported agent surface and operator scripts remain troubleshooting/maintenance surfaces.

**Patterns to follow:** Existing call-history docs added for issue #50; wrapper command lists in `README.md`, `SKILL.md`, and `references/api-reference.md`.

**Test scenarios:**

- Docs mention the new transcript command alongside `bin/list_calls.py`.
- Docs continue to route agents through `bin/` wrappers rather than generated CLI or operator scripts.
- Existing OpenClaw integration documentation tests remain green.

**Verification:** Documentation tests pass and manual doc sweep shows no agent-facing guidance pointing to operator-only transcript scripts.

---

## System-Wide Impact

- Agent CLI contract expands with one new supported command.
- Existing operator scripts remain available and should not be broken by helper extraction.
- No persistent storage, webhook behavior, SMS behavior, CRM behavior, or approval ledger behavior should change.

---

## Risks and Mitigations

- **Dialpad transcript source ambiguity:** The transcript endpoint and call-detail object may expose data differently. Mitigation: isolate fallback behavior in shared retrieval helpers and cover unavailable states explicitly.
- **Ambiguous recent-call selection:** `--last`/matching behavior can select the wrong call if used loosely. Mitigation: keep `--call-id` primary and rely only on existing lookup semantics for recent selection.
- **Scope creep into recap/follow-up:** Adjacent operator tooling exists for AI recap. Mitigation: tests and docs should keep transcript output separate from recap/coaching/follow-up fields.
- **Wrapper/operational script drift:** Promoting behavior into `bin/` can duplicate script logic. Mitigation: reuse or share existing helpers rather than copying formatting and API behavior wholesale.

---

## Verification Plan

- Unit tests for transcript formatting/retrieval helper behavior.
- Wrapper JSON contract tests for success, unavailable, invalid argument, and upstream failure paths.
- Documentation tests covering discoverability where existing doc tests apply.
- Full relevant Python test suite for touched call/transcript/wrapper areas.
- `git diff --check` before final commit.

---

## Assumptions

- Live Dialpad API verification is not required to implement the wrapper contract; mocked tests can validate behavior against known response shapes.
- Existing `DIALPAD_API_KEY` behavior remains the auth source for the new wrapper.
- Recent-call selection can reuse current call lookup behavior without designing a new matching system in this slice.

---

## Open Implementation Questions

- Which transcript source should be attempted first after implementation probes the current local generated CLI/API helpers: transcript endpoint, call detail `transcription_text`, or a fallback sequence?
- Should unavailable transcripts be represented as a successful wrapper result with `available: false` in all no-transcript cases, or should some no-transcript cases remain retryable wrapper errors?
