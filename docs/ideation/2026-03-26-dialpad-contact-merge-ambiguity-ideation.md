---
date: 2026-03-26
topic: dialpad-contact-merge-ambiguity
focus: avoid false contact merges for first-time inbound SMS/calls
---

# Ideation: Dialpad Contact Merge Ambiguity

## Codebase Context
The Dialpad skill already has a single enrichment seam in `scripts/webhook_server.py`: `lookup_contact_enrichment()` resolves one best-match contact name, `handle_webhook()` reuses that result for both hooks and Telegram, and the outbound prompt in OpenClaw can then act on that single name as if it were authoritative.

The repo has some useful guardrails already:
- `lookup_contact_enrichment()` distinguishes `resolved`, `not_found`, `unauthorized`, and degraded lookup states.
- `tests/test_sender_enrichment.py` already exercises degraded lookup and Markdown escaping.
- `THEORY.MD` explicitly says enrichment must be deterministic and reused once per event.
- Prior memory on the Bar Belle fix shows the same failure class: fitness-related name similarity beat stronger evidence and pointed a lead at the wrong business/software vendor.

The core gap is that the current flow still collapses uncertainty into one identity too early. A prompt tweak helps, but prompt-only safety is too weak if the runtime still returns a single best guess with no explicit ambiguity state.

## Ranked Ideas

### 1. Add a hard ambiguity gate before any merge/update
**Description:** Treat contact enrichment as “identification candidate” rather than “confirmed contact” unless the lookup has at least one strong identifier, such as exact phone match, exact email match, or another unambiguous primary key. If the evidence is only first name, industry, or a vague phone-area-code hint, mark the lead ambiguous and block auto-merge/update.
**Rationale:** This directly prevents the Reggie/Reggie Johnson failure mode. The system should fail closed on identity, not optimize for convenience.
**Downsides:** More ambiguous leads will need human review. That is the correct cost if the alternative is corrupting contacts.
**Confidence:** 96%
**Complexity:** Medium
**Status:** Unexplored

### 2. Return ranked contact candidates with evidence instead of one winner
**Description:** Change enrichment to return a small candidate list with scores and evidence snippets, not just a single `contact_name`. The agent can still draft a reply, but the UI or prompt sees the ambiguity explicitly and can ask for confirmation when the gap is small.
**Rationale:** This preserves useful enrichment while making uncertainty visible. It is stronger than a binary “resolved/not_found” result.
**Downsides:** Bigger payloads and more prompt logic. Also adds more surface area for formatting and tests.
**Confidence:** 88%
**Complexity:** Medium-High
**Status:** Unexplored

### 3. Add a contact identity lock with provenance
**Description:** Once a thread is linked to a contact, persist the evidence that justified that link and refuse to silently swap it to a different person with a similar name. If new evidence conflicts, surface a conflict state instead of replacing the contact.
**Rationale:** This stops stale context from drifting into a new identity and makes later updates auditable.
**Downsides:** State management gets more complicated. You need a clear rule for when a real identity change is legitimate.
**Confidence:** 82%
**Complexity:** Medium
**Status:** Unexplored

### 4. Make negative evidence explicit in the matching rules
**Description:** Encode “do not merge” signals such as first-name-only similarity, same vertical/keyword match, competitor/software vendor names, and weak phone clues like area code alone. If a candidate only survives because of one of those signals, force ambiguity.
**Rationale:** This targets the exact Reggie/Trainerize class of failure without requiring a giant ML system.
**Downsides:** Heuristics can get brittle and need maintenance as new false-positive patterns appear.
**Confidence:** 79%
**Complexity:** Low-Medium
**Status:** Unexplored

### 5. Separate enrichment from contact mutation
**Description:** Let the agent enrich, summarize, and draft replies, but require a separate confidence check before it can create/update the Dialpad contact. If confidence is low, the agent can still propose the update, but not execute it.
**Rationale:** This reduces blast radius. A wrong draft is annoying; a wrong merged contact is structural damage.
**Downsides:** Adds one more step for legitimate first-contact automation.
**Confidence:** 93%
**Complexity:** Low-Medium
**Status:** Unexplored

### 6. Add collision regression tests for real-world false matches
**Description:** Add tests for same-first-name/different-area-code collisions, fitness-vendor-vs-business collisions, and similar-name cases where exact identifiers disagree. Assert that these cases stay ambiguous and do not auto-merge.
**Rationale:** The repo already has a good test seam for sender enrichment. This is the cheapest way to stop the specific regression from coming back.
**Downsides:** Tests only help if the runtime exposes the right state. They are enforcement, not design.
**Confidence:** 97%
**Complexity:** Low
**Status:** Unexplored

### 7. Tighten the OpenClaw prompt, but treat it as a backstop
**Description:** Add an explicit system-prompt rule for `niemand-work`: never merge contacts on first name, industry, or area code alone; ask for confirmation or keep the lead ambiguous when evidence conflicts.
**Rationale:** The prompt is the last-mile policy layer, and it is worth hardening. It will stop some bad actions even if the runtime code misses a case.
**Downsides:** Prompt-only fixes drift. They are a backstop, not the primary safety mechanism.
**Confidence:** 72%
**Complexity:** Low
**Status:** Unexplored

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | Auto-merge based on area-code or industry weighting | Too brittle. That is exactly how the Reggie merge happened. |
| 2 | “Just make the prompt smarter” | Insufficient by itself. Prompt drift and stale context are not reliable safety controls. |
| 3 | Blindly prefer the most recent contact name in history | History can be stale or wrong; recency is not identity. |
| 4 | Web-search-only identity resolution | Slower, noisier, and still vulnerable to the same ambiguous-name problem. |
| 5 | Keep the first-contact thread but never update Dialpad contacts | Safer than wrong merges, but throws away a useful automation path instead of fixing it. |

## Session Log
- 2026-03-26: Initial ideation - 7 ideas generated, 7 retained for review
