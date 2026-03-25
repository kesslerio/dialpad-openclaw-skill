# Plan: Agent Interface Documentation Boundaries

Date: 2026-03-25
Issues: #33, #36
Depth: Lightweight

## Problem Frame

The repository still presents three different surfaces as if they were equally valid for agent use:

- `bin/*` wrappers as the preferred interface
- `generated/dialpad` as a normal direct interface
- `scripts/*` commands mixed into general usage examples

That contradicts the intended safety model. Agents need one deterministic contract. The generated CLI is too broad and noisy to be a supported agent entrypoint, and operational scripts should remain available without being framed as first-class agent commands.

## Scope

In scope:

- Reposition `bin/*` wrappers as the only supported agent-facing interface
- Reframe `generated/dialpad` as an internal backend surface for wrappers and manual operator troubleshooting
- Separate operator-only `scripts/*` usage from agent usage in top-level docs
- Align README, SKILL, and reference docs so they stop drifting on this point

Out of scope:

- Changing wrapper behavior or generated CLI implementation
- Removing scripts or generated assets from the repository
- Adding new wrappers for missing capabilities
- Changing webhook or storage runtime behavior

## Requirements Trace

### Issue #33

- `README.md` and `SKILL.md` must state that `bin/*` is the supported agent interface
- `scripts/*` examples must not be presented as equivalent to core agent commands
- Architecture docs must describe `bin/*` as the stable contract

### Issue #36

- `generated/dialpad` must be framed as an internal implementation detail for agent usage
- Direct generated CLI usage must be limited to advanced/manual operator troubleshooting
- Wrapper-to-generated flow must be explicit in architecture/docs

## Current Evidence

- `README.md` includes direct `generated/dialpad` auth preflight and direct CLI guidance in notes
- `SKILL.md` quick start begins with direct generated CLI auth preflight and lists `generated/dialpad` as an execution mode
- `references/api-reference.md` documents generated CLI capabilities without clearly separating operator/manual use from agent-safe use
- `references/architecture.md` correctly shows wrappers calling the generated CLI, but does not explicitly define the generated CLI as internal-only for agent workflows

## Decisions

1. `bin/*` will be documented as the only supported agent-facing command surface.
   Rationale: This directly satisfies both issues and keeps LLM invocation deterministic.

2. `generated/dialpad` will remain documented, but only in an explicit operator/manual troubleshooting context.
   Rationale: The repo still needs to support human debugging and regeneration workflows without advertising the raw CLI to agents.

3. `scripts/*` will remain documented only as operational tooling.
   Rationale: Webhook, SQLite, and storage flows are legitimate operator tasks, but they should not compete with wrapper guidance.

4. The implementation will stay documentation-only unless a contradiction requires a tiny metadata cleanup.
   Rationale: The issues are about positioning and supported surfaces, not runtime behavior.

## Files

- `README.md`
- `SKILL.md`
- `references/api-reference.md`
- `references/architecture.md`

## Implementation Units

### Unit 1: Top-Level Contract Alignment

Goal:
- Make the wrapper-first contract explicit in the top-level docs

Files:
- `README.md`
- `SKILL.md`

Approach:
- Replace direct generated CLI quick-start framing with wrapper-first setup and usage
- Add a short operator/troubleshooting section that explains when `generated/dialpad` is acceptable
- Move `scripts/*` examples into clearly labeled operational sections

Patterns to follow:
- Keep README concise and navigational
- Keep SKILL focused on agent-safe usage

Verification:
- No README or SKILL section implies that agents should call `generated/dialpad` directly for normal tasks
- `scripts/*` examples are labeled operator/operational rather than agent-facing

### Unit 2: Reference Doc Consistency

Goal:
- Align deeper docs with the same interface boundary

Files:
- `references/api-reference.md`
- `references/architecture.md`

Approach:
- Reframe generated CLI sections as internal backend surface plus manual operator tooling
- State wrapper-to-generated flow plainly in architecture
- Preserve useful advanced reference material, but relocate its audience to operators/manual troubleshooting

Patterns to follow:
- Keep reference docs descriptive rather than marketing-heavy
- Avoid deleting valid operational knowledge

Verification:
- Reference docs consistently describe wrappers as the stable agent contract
- Generated CLI references are explicitly manual/operator-facing

## Risks

- Overcorrecting and hiding legitimate operator workflows
- Leaving one stray example that reintroduces ambiguity
- Repeating the same guidance differently across docs and creating new drift

## Test Scenarios

1. Scan `README.md`, `SKILL.md`, `references/api-reference.md`, and `references/architecture.md` for `generated/dialpad` mentions and verify each mention is either internal/backend or manual/operator-facing.
2. Scan for `scripts/` examples and verify they are labeled operational, not core agent commands.
3. Confirm top-level command examples for SMS, calls, contacts, and webhooks use `bin/*` wrappers where wrappers exist.

## Verification

- Text search for interface-surface phrasing across the edited docs
- Manual review of doc flow for contradiction removal

## Alternative Considered

Close #33 and #36 as already satisfied.

Rejected because the current docs still explicitly advertise direct generated CLI use and therefore do not actually meet the acceptance criteria.
