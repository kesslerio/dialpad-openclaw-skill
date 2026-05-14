---
date: 2026-05-14
topic: call-transcript-wrapper
---

# Call Transcript Wrapper

## Summary

Dialpad call transcripts should be available through the supported agent-facing command surface. Agents should be able to retrieve transcript text for a known or recently selected call without using operator-only scripts, generated CLI internals, or raw API calls.

---

## Problem Frame

The current agent-facing call history command exposes useful call metadata, including call id, contact, duration, status, and recording links. It does not expose the spoken transcript. When an agent needs to review what was said on a call, the documented path stops at metadata and pushes the agent toward unsupported surfaces or manual API work.

This is a product gap in the skill contract, not primarily an agent knowledge problem. Existing repo guidance tells agents to use `bin/` wrappers for normal work and treat scripts/generated tools as operator-only troubleshooting surfaces.

---

## Actors

- A1. Agent: Uses the Dialpad skill to inspect call history and retrieve call details.
- A2. Operator: Relies on the agent to summarize or act on call context.
- A3. Dialpad skill: Provides the supported wrapper contract between agents and Dialpad.

---

## Key Flows

- F1. Retrieve a transcript for a known call
  - **Trigger:** An agent has a Dialpad call id from call history or another source.
  - **Actors:** A1, A3
  - **Steps:** The agent runs an agent-facing transcript command with the call id, receives normalized transcript output, and can use the text in downstream reasoning.
  - **Outcome:** The agent can review call content without leaving the supported wrapper surface.
  - **Covered by:** R1, R2, R3, R5

- F2. Transcript is unavailable
  - **Trigger:** A call has no transcript, transcription is still processing, or Dialpad does not expose transcript data for that call.
  - **Actors:** A1, A3
  - **Steps:** The agent requests the transcript and receives a clear unavailable result rather than an ambiguous failure.
  - **Outcome:** The agent knows the transcript cannot be used and can fall back to recording links or operator follow-up.
  - **Covered by:** R4, R6

---

## Requirements

**Agent-facing access**
- R1. The skill must provide a supported agent-facing command for retrieving a transcript for a single Dialpad call.
- R2. The command must accept a known call id as the primary selection mode.
- R3. The command should support selecting a recent call when that can be done without ambiguity, using the same general behavior agents already expect from call history tooling.

**Output behavior**
- R4. When no transcript is available, the command must return a graceful, explicit unavailable result rather than implying success with empty content.
- R5. Machine-readable output must include the call id, transcript text when available, and enough call metadata to confirm the transcript belongs to the intended call.
- R6. Human-readable output must be concise and distinguish "no transcript" from API/configuration failures.

**Scope discipline**
- R7. The transcript command must remain a retrieval utility, not a recap, coaching, CRM enrichment, or suggested follow-up workflow.
- R8. The supported docs must make the transcript path discoverable wherever call history workflows are documented.

---

## Acceptance Examples

- AE1. **Covers R1, R2, R5.** Given an agent has a call id from call history, when it requests the transcript, then it receives transcript text plus identifying call metadata through the supported wrapper surface.
- AE2. **Covers R3, R5.** Given an agent asks for the most recent matching call and the selection is unambiguous, when it requests the transcript, then the command resolves the call and returns the transcript result for that call.
- AE3. **Covers R4, R6.** Given a call exists but has no transcript yet, when the agent requests the transcript, then the response explicitly says the transcript is unavailable and does not present empty text as a successful transcript.
- AE4. **Covers R7.** Given a transcript is returned, when the command completes, then it does not add AI recap, suggested replies, CRM updates, or sales coaching output.

---

## Success Criteria

- Agents can retrieve call transcript text without raw Dialpad API calls or operator-only tools.
- The documented call workflow clearly covers both metadata lookup and transcript retrieval.
- Missing transcripts produce clear, actionable outcomes instead of silent empty output.
- Planning can proceed without expanding the work into call recap, follow-up drafting, or CRM enrichment.

---

## Scope Boundaries

- Not AI recap retrieval.
- Not suggested follow-up drafting.
- Not CRM enrichment or contact synchronization.
- Not bulk transcript export, search, or indexing.
- Not a change to call history metadata listing beyond any discoverability needed for transcript retrieval.
- Not a replacement for recording links when Dialpad has no transcript.

---

## Key Decisions

- V1 is transcript-only to keep the feature small and avoid coupling it to broader sales intelligence workflows.
- The feature belongs in the agent-facing wrapper contract because current repo guidance intentionally keeps agents out of operator-only scripts and generated CLI internals.
- Graceful unavailable states are part of the core requirement because Dialpad transcripts may be absent or still processing.

---

## Dependencies / Assumptions

- Dialpad exposes transcript data for at least some calls through its call/transcript APIs.
- Existing call history output provides call ids or enough selection context for a transcript command to target the intended call.
- Existing authentication behavior for call history should be sufficient for transcript retrieval.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R1, R4][Needs research] Which Dialpad transcript source should be tried first, and what fallback behavior is needed when one source returns not found?
- [Affects R3][Technical] What exact recent-call selection options should be exposed without making ambiguous matches look deterministic?
