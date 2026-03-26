---
title: bug: stale "Already sent" replies need current-turn verification
type: bug
status: completed
date: 2026-03-26
origin: https://github.com/kesslerio/dialpad-openclaw-skill/issues/64
depth: standard
---

# bug: Stale "Already sent" Replies Need Current-Turn Verification

## Overview

Issue #64 is not the earlier SMS status-label bug. The Dialpad send wrappers can be working correctly while the OpenClaw operator still claims success from stale session context. The failure mode is behavioral: `niemand-work` answered "Already sent." without a fresh `send_sms.py` or `update_contact.py` call in the current turn, which makes the operator trust the wrong state.

The fix belongs in the live OpenClaw operator contract, not in the Dialpad send wrapper. This repo should tighten the prompt boundary, document the freshness rule, and add a small regression around the written contract so the same failure is harder to reintroduce.

## Problem Frame

Operators need current-turn truth, not memory-shaped confidence. When a user asks to send a message or update a contact, the assistant must either:

- perform the action in the current turn, or
- explicitly state that it has not verified the current turn yet and proceed to verify it

The current behavior shortcuts that requirement and answers "Already sent." from stale context. That hides real failures and makes the tool layer look trustworthy when it has not been exercised.

## Requirements Trace

- R1. A fresh current-turn tool call is required before any success claim about sending or updating.
- R2. Prior turn context must not satisfy a new send/update request.
- R3. "Already sent" is only valid after the current turn has actually verified the action.
- R4. The rule applies equally to `send_sms.py` and `update_contact.py`.
- R5. Repo docs must describe the freshness rule and separate draft generation from send authority.
- R6. The repo must not conflate this bug with issue #58, which was only about SMS status wording.

## Scope Boundaries

In scope:

- tighten the live `niemand-work` prompt in OpenClaw
- update repo docs so the operator contract is explicit
- add a lightweight regression around the written contract

Out of scope:

- changing Dialpad API semantics
- changing `send_sms.py` delivery behavior
- reworking first-contact enrichment
- adding a general long-term memory system
- broadening this into a new agent architecture

## Context & Research

### Relevant Code and Docs

- `README.md` and `SKILL.md` already define `bin/*.py` as the supported Dialpad agent surface and point to `niemand-work` as the OpenClaw handoff target.
- `references/openclaw-integration.md` already says to separate draft generation from send authority and default to `approval_required`.
- `references/api-reference.md` already documents `firstContact`, `autoReply`, and explicit `niemand-work` routing.
- The live OpenClaw config in `~/.openclaw/openclaw.json` contains the `Dialpad Operations` prompt for `telegram:group:-1003882776023`.
- `tests/test_webhook_hooks.py` and `tests/test_sender_enrichment.py` already lock hook payload structure, but there is no repo test for stale success narration.
- No relevant `docs/solutions/` learning exists in this repo for this specific workflow bug.

### Source Evidence

- The issue trace in `~/.openclaw/agents/niemand-work/sessions/8b39045a-630a-4cf6-a9e0-b1f1f6211615.jsonl` shows the later Reggie turn returned "Already sent." without a fresh send/update tool call.
- The SMS database row for the actual outbound message exists separately and does not justify a stale follow-up success claim.

## Key Technical Decisions

1. Fix the live prompt first, not the wrapper.
   Rationale: the bug is stale narration in the OpenClaw agent, not a broken Dialpad send path.

2. Make verification current-turn explicit.
   Rationale: the agent needs to distinguish "I drafted this earlier" from "I just executed this now."

3. Treat "Already sent" as forbidden unless a current-turn tool result proves it.
   Rationale: this is the exact false claim in issue #64.

4. Document the freshness rule in the repo contract.
   Rationale: operators and downstream maintainers need the same wording that the live prompt enforces.

5. Add a small contract test for the docs.
   Rationale: the prompt itself lives outside the repo, but the repo can still lock the contract language that keeps this behavior honest.

## Open Questions

Resolved during planning:

- Is this the same bug as issue #58? No. #58 was about wrapper status wording; #64 is about stale success narration.
- Should the fallback wording be "Already sent"? No. If the current turn has not verified the action, the agent should say it has not verified this turn yet.

Deferred to implementation:

- Whether the live prompt should also keep a compact last-tool-call fingerprint in session state.
- Whether the docs should include a short example of the forbidden stale-success path versus the correct verification-first path.

## Implementation Units

- [ ] **Unit 1: Tighten the live `niemand-work` prompt**

**Goal:** Make the `Dialpad Operations` prompt refuse stale success claims and require current-turn verification before saying a send or update is already done.

**Requirements:** R1, R2, R3, R4, R6

**Files:**
- Modify: `~/.openclaw/openclaw.json`

**Approach:**
- Update the `Dialpad Operations` system prompt for `telegram:group:-1003882776023` so it explicitly requires a fresh tool call in the current turn before any success claim about `send_sms.py` or `update_contact.py`.
- Add a hard rule that stale context cannot answer "Already sent." or "Already updated." unless the current turn has a matching tool result.
- Define a fallback phrase such as "I have not verified this turn yet" for cases where the assistant is about to act but has not yet executed a tool call.
- Keep the existing `attio`, `web_search`, `humanizer`, and Dialpad wrapper routing intact; this is a freshness guardrail, not a new workflow.

**Patterns to follow:**
- The current `Dialpad Operations thread` prompt in `~/.openclaw/openclaw.json`
- The existing `OpenClaw` integration guidance in this repo

**Test scenarios:**
- A repeated request in the same session no longer short-circuits to "Already sent." without a fresh tool call.
- A current-turn send request causes the agent to either run `send_sms.py` or clearly state that it has not verified the turn yet.
- A current-turn contact-sync request behaves the same way for `update_contact.py`.

**Verification:**
- Live operator smoke test against `niemand-work` shows current-turn verification instead of stale success narration.

- [ ] **Unit 2: Document the freshness contract in the repo**

**Goal:** Make the repo docs say the same thing the live prompt will enforce, so the behavior is obvious to operators and future maintainers.

**Requirements:** R3, R5, R6

**Files:**
- Modify: `README.md`
- Modify: `SKILL.md`
- Modify: `references/openclaw-integration.md`
- Modify: `references/api-reference.md`
- Test: `tests/test_openclaw_integration_docs.py`

**Approach:**
- Add a short "current-turn verification" note to the Dialpad Operations guidance.
- Clarify that "Already sent" is only valid after a fresh tool call in the current turn, not from memory.
- Keep the separation between draft generation, send authority, and verified execution explicit.
- Keep issue #58 and issue #64 clearly distinct in the prose so future readers do not merge the bugs.

**Patterns to follow:**
- The current human-in-the-loop sections in `references/openclaw-integration.md`
- The wrapper guidance in `README.md` and `SKILL.md`
- The existing `firstContact` / `autoReply` contract language in `references/api-reference.md`

**Test scenarios:**
- The docs explicitly mention current-turn verification and stale-context prohibition.
- The docs still preserve the "draft generation vs send authority" split.
- The docs do not confuse this bug with the earlier SMS status-label fix.

**Verification:**
- The new doc contract test passes and the docs read as a single, consistent operator policy.

## System-Wide Impact

- The actual behavior change lives in the OpenClaw operator config, so the repo plan needs a live config reload after the prompt update.
- The repo docs should keep future operators from reintroducing stale success language in other instructions or handoff notes.

## Risks & Dependencies

- The main risk is prompt drift: if the live `niemand-work` prompt and the repo docs diverge, the stale-success bug can return.
- Another risk is over-correction: the agent still needs to act, not just disclaim. The fallback wording must not become a dead end.
- A third risk is scope creep into memory systems or new agent architecture. This issue only needs a freshness gate and clearer operator wording.

## Verification

- Confirm that a current-turn send/update request no longer produces "Already sent." from stale context alone.
- Confirm that the docs and live prompt both describe the same freshness rule.
- Confirm that issue #58 remains separate and unchanged by this work.

## Sources & References

- **Origin issue:** [#64](https://github.com/kesslerio/dialpad-openclaw-skill/issues/64)
- Related repo docs: [README.md](/home/art/projects/skills/work/dialpad-openclaw-skill/README.md), [SKILL.md](/home/art/projects/skills/work/dialpad-openclaw-skill/SKILL.md), [references/openclaw-integration.md](/home/art/projects/skills/work/dialpad-openclaw-skill/references/openclaw-integration.md), [references/api-reference.md](/home/art/projects/skills/work/dialpad-openclaw-skill/references/api-reference.md)
- Related live config: `/home/art/.openclaw/openclaw.json`
- Related trace: `/home/art/.openclaw/agents/niemand-work/sessions/8b39045a-630a-4cf6-a9e0-b1f1f6211615.jsonl`
