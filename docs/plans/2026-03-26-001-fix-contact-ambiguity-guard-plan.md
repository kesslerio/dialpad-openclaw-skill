---
title: fix: Guard ambiguous first-contact identity
type: fix
status: completed
date: 2026-03-26
origin: docs/brainstorms/2026-03-26-dialpad-contact-ambiguity-guard-requirements.md
---

# fix: Guard ambiguous first-contact identity

## Overview

The Reggie false-merge showed that first-contact automation is still collapsing weak signals into one identity too early. The fix belongs primarily in the agent/group instruction layer and the Attio/ShapeScale CRM contract, with Dialpad acting as the ingress surface that preserves evidence and passes it through. Ambiguous identity must stay explicit, draftable, and non-mutating until the evidence is genuinely strong.

## Problem Frame

(see origin: `docs/brainstorms/2026-03-26-dialpad-contact-ambiguity-guard-requirements.md`)

First-contact enrichment currently behaves like a single-winner resolver. That is too aggressive for real inbound leads, especially when the only clues are first name, area code, industry, or other soft similarity. The system should fail closed on identity: draft a reply if useful, but do not merge or update a contact unless the evidence is strong enough to justify it.

## Requirements Trace

- R1. First-contact enrichment must distinguish `resolved`, `ambiguous`, `not_found`, and `degraded` identity states.
- R2. Do not auto-merge or auto-update a contact unless the identity is backed by strong evidence such as an exact phone match, exact email match, or another equally strong primary key.
- R3. First name, area code, industry, job title, and similar soft signals must never be sufficient on their own.
- R4. When identity is ambiguous, the system may still draft a reply or surface context, but it must not mutate the contact record.
- R5. If a thread is already linked to a contact, later conflicting evidence must be treated as a conflict, not silently replaced with a similar contact.
- R6. Preserve provenance for the contact decision so the operator can see why the person was resolved, ambiguous, or blocked.
- R7. Keep the live OpenClaw prompt aligned with the repo contract, but treat the prompt as a backstop rather than the primary safety mechanism.
- R8. Add regression coverage for false-match cases, including same-first-name collisions, same-industry collisions, and weak area-code-only similarity.

## Scope Boundaries

- No fuzzy-matching rewrite.
- No ML ranking system.
- No broad Dialpad wrapper refactor; Dialpad should remain a minimal ingress and pass-through layer.
- No automatic mutation on soft signals such as first name, area code, or industry.
- No attempt to solve every CRM dedupe problem in the company, only the first-contact ambiguity class covered by the origin doc.

## Context & Research

### Relevant Code and Patterns

- `scripts/webhook_server.py` for the current ingress seam, sender enrichment, and OpenClaw hook payload construction.
- `tests/test_sender_enrichment.py`, `tests/test_webhook_hooks.py`, and `tests/test_webhook_server.py` for webhook and enrichment behavior.
- `THEORY.MD` for the control-plane model: one inbound event should produce one eligibility decision that downstream sinks reuse.
- `~/.openclaw/openclaw.json` for the live `niemand-work` / Dialpad Operations instruction layer.
- `shapescale-crm/SKILL.md`, `references/company_workflows.md`, `references/deal_workflows.md`, and `references/field_validation_guide.md` for the CRM-side mutation and note-routing contract.
- `attio-crm/SKILL.md` and `attio-crm/references/field_validation_guide.md` for the mirrored Attio contract.

### Institutional Learnings

- `docs/solutions/integration-issues/demo-followup-attio-rust-client-hardening.md` shows why transport and behavior parity need explicit contracts rather than implied behavior.
- `shapescale-crm/scripts/pipeline_hygiene.py` and its tests already encode a strong-identity gate via `uses_strong_identity` and `needs_human_review`; this plan should mirror that pattern instead of inventing a new one.
- `~/.openclaw/agents/niemand-work/memory/2026-03-17-bar-belle-crm-fix.md` captures the same failure shape: fitness-themed similarity beat stronger evidence and polluted the wrong record.

## High-Level Technical Design

This is directional guidance, not implementation specification.

```text
Inbound event
  -> sender/contact enrichment
  -> explicit identity state (`resolved` | `ambiguous` | `not_found` | `degraded`)
  -> agent/group policy
  -> CRM mutation gate
  -> draft reply / note / update
```

- If identity is `resolved` with strong evidence, allow mutation and downstream reply drafting.
- If identity is `ambiguous`, keep the lead useful but block any contact mutation.
- If identity is `not_found` or `degraded`, preserve that status rather than flattening it into a false certainty.
- The agent prompt can continue work, but it should not be the source of truth for identity strength; the CRM contract should enforce the gate.

## Alternative Approaches Considered

- Prompt-only safety: rejected because it is too easy to drift and too weak to protect against future prompt changes.
- Dialpad-only heuristics: rejected because Dialpad is the ingress surface, not the right place to decide identity.
- Candidate list + manual confirmation for everything: useful as a future enhancement, but heavier than needed for this fix.

## Implementation Units

### 1. Harden the live agent/group prompt

- **Goal:** Update the live OpenClaw instruction layer so `niemand-work` treats strong identity evidence as mandatory before any contact mutation and refuses to claim success from stale context.
- **Requirements:** R1, R2, R3, R4, R5, R6, R7.
- **Dependencies:** The current `Dialpad Operations` prompt in `~/.openclaw/openclaw.json`.
- **Files:** `~/.openclaw/openclaw.json`, `tests/test_sender_enrichment.py`, `tests/test_webhook_server.py`.
- **Approach:** Make the prompt explicitly say that soft signals are insufficient, ambiguous leads are draft-only, and mutation claims require a fresh current-turn tool result. Keep the existing current-turn verification rule, but tighten the identity gate around it.
- **Patterns to follow:** The current `Dialpad Operations thread` prompt structure and the existing "current-turn verification" language.
- **Test scenarios:** Same-first-name/different-area-code contacts stay ambiguous; prior-session context does not justify "Already sent" or "Already updated"; a weak fitness-themed similarity does not become a resolved match.
- **Verification:** The live prompt states the stronger gate unambiguously, and the behavior tests still pass with ambiguous cases preserved rather than collapsed.

### 2. Align the repo-facing docs with the same policy

- **Goal:** Make the Dialpad skill docs describe Dialpad as an ingress surface, not the identity authority, and document the ambiguity state as a first-class outcome.
- **Requirements:** R1, R4, R6, R7.
- **Dependencies:** The current `README.md`, `SKILL.md`, `references/api-reference.md`, `references/openclaw-integration.md`, and `THEORY.MD`.
- **Files:** `README.md`, `SKILL.md`, `references/api-reference.md`, `references/openclaw-integration.md`, `THEORY.MD`, `tests/test_webhook_hooks.py`.
- **Approach:** Update the written contract so the repo docs and live prompt agree on the same identity states and the same prohibition on weak-signal mutation. Keep the Dialpad docs narrow: preserve evidence, forward the event, and do not imply the wrapper can safely auto-merge on soft signals.
- **Patterns to follow:** The existing explicit contract style in `THEORY.MD`, which already frames the wrapper as a control plane with one shared policy decision per inbound event.
- **Test scenarios:** The examples and API reference talk about `resolved`/`ambiguous`/`not_found`/`degraded` rather than a binary resolved/unresolved model; docs do not suggest that first name or area code alone can trigger a mutation.
- **Verification:** The repo-facing guidance matches the live agent prompt and does not present Dialpad as the component that decides identity.

### 3. Encode identity guardrails in the CRM skill contracts

- **Goal:** Make the Attio/ShapeScale CRM skills enforce the same strong-identity rule set so that update/create flows stay fail-closed.
- **Requirements:** R2, R3, R4, R5, R6.
- **Dependencies:** The company and deal workflow docs plus the field-validation guides.
- **Files:** `/home/art/projects/shapescale-openclaw-skills/shapescale-crm/SKILL.md`, `/home/art/projects/shapescale-openclaw-skills/shapescale-crm/references/company_workflows.md`, `/home/art/projects/shapescale-openclaw-skills/shapescale-crm/references/deal_workflows.md`, `/home/art/projects/shapescale-openclaw-skills/shapescale-crm/references/field_validation_guide.md`, `/home/art/ShapeScaleAI/claude-desktop-skills/build/attio-crm/SKILL.md`, `/home/art/ShapeScaleAI/claude-desktop-skills/build/attio-crm/references/field_validation_guide.md`.
- **Approach:** State that uncertain identity belongs in notes or candidate records, not in `update_record`. Preserve the existing `create_note`-instead-of-update pattern for uncertain research, and make the strong-identity gate explicit before any person/company relationship mutation.
- **Patterns to follow:** `create_note` when data is uncertain, `main_contact` as a UUID-only relationship field, and the `uses_strong_identity` / `needs_human_review` convention already present in `pipeline_hygiene`.
- **Test scenarios:** Same-name people with different area codes remain separate; same-industry similarity does not justify a merge; exact phone/email or another equally strong key can still support a safe update; conflicting evidence forces review instead of overwrite.
- **Verification:** The CRM skill docs tell the operator to fail closed on weak evidence and to use notes instead of writes when identity is not strong enough.

### 4. Add regression coverage for false matches and ambiguity preservation

- **Goal:** Lock in the failure mode so the Reggie-style collision cannot quietly return later.
- **Requirements:** R1, R2, R3, R4, R5, R8.
- **Dependencies:** The prompt/doc changes above, plus the existing webhook and pipeline hygiene tests.
- **Files:** `tests/test_sender_enrichment.py`, `tests/test_webhook_hooks.py`, `tests/test_webhook_server.py`, `/home/art/projects/shapescale-openclaw-skills/shapescale-crm/tests/test_pipeline_hygiene.py`, `/home/art/projects/shapescale-openclaw-skills/shapescale-crm/tests/test_pipeline_verify.py`.
- **Approach:** Cover same-first-name/different-number, same-area-code/different-person, and fitness-context false positives. Ensure the ambiguity state survives the handoff into the hook payload and that CRM hygiene still treats weak identity as human-review territory.
- **Patterns to follow:** The current characterization style in the webhook tests and the strong-identity assertions already present in `pipeline_hygiene` tests.
- **Test scenarios:** Weak matches stay ambiguous; strong exact matches still resolve; provenance/status fields survive the handoff; stale success claims do not appear without current-turn tool evidence.
- **Verification:** The regression suite fails if a soft-signal match is allowed to mutate a contact or if ambiguity gets flattened away before the CRM layer sees it.

## Risks and Mitigations

- **Risk:** Over-blocking legitimate first contacts.
  - **Mitigation:** Require only strong primary keys for mutation; ambiguous leads can still get draft replies and notes.
- **Risk:** Runtime/docs drift because `~/.openclaw/openclaw.json` is outside the repo.
  - **Mitigation:** Update the live prompt and repo docs together, and treat the prompt as a backstop rather than the only defense.
- **Risk:** The CRM side may have slightly different field/schema constraints than the Dialpad ingress layer.
  - **Mitigation:** Use notes for uncertain or forbidden fields and only mutate verified fields.

## Success Metrics

- No same-first-name / different-area-code false merge can mutate a contact.
- Ambiguous first contacts still produce useful drafts and context.
- The live prompt and repo docs use the same identity-state vocabulary.
- Regression tests fail if weak evidence is allowed to create or update a contact.

## Verification Plan

- The live OpenClaw prompt is updated to require strong identity evidence before mutation.
- The Dialpad skill docs and CRM skill docs agree on the same ambiguity rule set.
- The regression suite covers both the webhook ingress path and the CRM hygiene path.
- The plan remains valid even if future implementation details differ, as long as the safety boundary stays at the agent/CRM contract rather than the Dialpad wrapper.
