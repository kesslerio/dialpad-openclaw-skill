# Goal: Autonomously land PR2 → PR3 → S2–S6 (Dialpad enrichment program)

```yaml
goal_id: dialpad-enrichment-autoloop
created: 2026-06-18
repo: kesslerio/dialpad-openclaw-skill
default_branch: main
source_docs:
  ideation: docs/ideation/2026-06-18-dialpad-auto-response-enrichment.md
  plan_s1: docs/plans/2026-06-18-001-feat-dialpad-enrichment-wiring-plan.md
config:
  pr_loc_hard_cap: 1000        # additions+deletions; split before exceeding
  pr_loc_target: 450           # aim here; treat >800 as a split signal
  docs_in_separate_pr: true    # planning/reference/compound docs never ride with code
  review_rounds_cap: 4         # per PR, across all reviewers, before surfacing residuals
  auto_merge: low_risk_only    # see Risk tiers; everything else is human-gated
  squash_merge_only: true
status: not_started
```

> **Read this first, every iteration.** This document is the runbook AND the
> state. The loop is stateless between iterations except for git history and the
> **Progress ledger** at the bottom. Each iteration: orient → pick the next ready
> item → execute the per-item procedure → update the ledger → self-pace to next,
> or stop at a checkpoint.

---

## Objective

Land the remaining Dialpad enrichment program with minimum human intervention,
while never autonomously shipping a change to the **live customer-facing SMS
path** without an explicit human gate. The loop does all of the building,
testing, review, and review-fix work autonomously; it asks the human only at the
genuine decision points (merge/deploy of customer-facing changes, enabling
auto-send, and irreducibly-ambiguous product decisions).

---

## Operating principles

1. **Sequential, right-sized PRs.** One backlog item → one (or a few) focused
   PRs. Target ~450 LOC, **hard cap < 1000 LOC** (additions+deletions vs. merge
   base). If a unit set would exceed the cap, split into sequential sub-PRs by
   the plan's Implementation Units.
2. **Docs ride alone.** Planning docs, reference docs, ledger updates, and
   `ce-compound` learnings go in their **own** PRs, never mixed with code.
3. **Review until clean.** No PR advances to merge-eligibility until the review
   gate (below) reports **zero actionable findings in a single round** across
   `/autoreview`, `/thermo-nuclear-code-quality-review`, and any GitHub PR review
   (e.g. Codex).
4. **Honor the invariants.** Every change to message delivery, idempotency,
   routing, send behavior, or background jobs must state the invariant it
   preserves and prove it with a test that asserts the invariant — not just the
   path (per repo + global Product Security Judgment rules).
5. **Branch off `main` per item.** Never branch off an unmerged PR. If the next
   item depends on an unmerged PR, the loop is at a checkpoint — stop and surface.
6. **No live `.env` / `systemctl restart` / auto-send enablement without a human
   gate.** These are CP2/CP3 below. The loop may *prepare* them (documented diffs)
   but never *apply* them autonomously.

---

## Risk tiers (drive the merge policy)

- **Auto-merge eligible** (loop may `gh pr merge --squash` once review-clean AND
  CI green — or where there is no CI, once review-clean):
  - Additive code with no effect on the live request/draft/send path.
  - Observability / read-only analysis.
  - Docs and `ce-compound` PRs.
  - In this program: **S2** (Attio/identity resolver library — importable, not yet
    wired into the webhook), **S6** (approval-DB eval + pulse, read-only), all docs.
- **Human-gated** (loop opens the PR, drives it to review-clean, then **STOPS** at
  a checkpoint for explicit approval before merge and/or deploy):
  - Anything modifying `webhook_server.py` request handling, draft generation,
    draft content, routing, or send behavior.
  - Anything requiring a live-service deploy (`.env` activation + restart).
  - Anything enabling auto-send.
  - In this program: **PR2** (async + idempotency — changes the live webhook
    behavior), **PR3** (un-gate + provenance + deploy), **S3** (branching changes
    draft content), **S4** (auto-send), **S5** (routing + Attio write-back).

---

## Backlog (ordered; respect dependencies)

| # | Item | Source | Depends on | Risk | Suggested PR split |
|---|------|--------|-----------|------|--------------------|
| 1 | **PR2 — async draft gen + SMS idempotency** | plan_s1 U5, U6 | S1 (PR #90 merged) | human-gated | One PR if < cap; else U5 then U6 |
| 2 | **PR3 — un-gate into draft lane + provenance, then deploy** | plan_s1 U7, U8 | PR2 merged | human-gated | U7 (code) PR; U8 (deploy) is CP2, not a code PR |
| 3 | **S2 — phone-first identity resolver** | ideation S2 | S1 adapter (`scripts/adapters/attio_context.py`) | auto-merge | Cascade core PR; caching/provenance PR if large |
| 4 | **S3 — customer/prospect branching off Attio + QMD** | ideation S3 | S2, PR3 | human-gated | One PR (draft-content change) |
| 5 | **S4 — confidence-gated auto-send + risk classifier (shadow first)** | ideation S4 | S6 (eval), S3 | human-gated | Shadow-mode PR; graduation is CP3 |
| 6 | **S5 — line-based routing + Attio write-back** | ideation S5 | PR2 | human-gated | Routing PR; write-back PR |
| 7 | **S6 — approval-DB eval + weekly pulse** | ideation S6 | none (pull forward when gated items wait) | auto-merge | Eval PR; pulse PR |

**Pre-plan requirement for S2–S6:** these exist only as ideation survivors. Before
`/lfg`, each needs a plan. The loop runs `/ce-plan` against the ideation doc
(bootstrap mode, recording explicit assumptions) — autonomous. Escalate to
`/ce-brainstorm` (which pauses for the human, **CP4**) only when an item carries a
product decision the loop cannot responsibly assume (e.g. S4's auto-send
risk-tolerance, S5's exact sales/work line mapping).

**S6 may be pulled forward** whenever an earlier item is parked at a human
checkpoint, since it is independent and auto-merge eligible — keeps the loop
productive while waiting on the human.

---

## Per-item procedure (the loop body)

For the next ready backlog item:

1. **Orient.** Read the Progress ledger and `gh pr list --state all` +
   `git log main`. Determine the next item whose dependencies are merged and that
   is not done/parked. If none ready → if all done, **stop** (omit the wakeup);
   else surface the blocking checkpoint and **stop**.
2. **Plan.** Ensure a plan exists for the item (PR2/PR3 already in `plan_s1`).
   For S2–S6, if absent, run `/ce-plan` (bootstrap, explicit assumptions). If the
   item needs a product decision → `/ce-brainstorm` → **CP4 stop**. Plans land in
   their own docs PR (auto-merge).
3. **Size check.** Estimate the change. If projected > 800 LOC, split into
   sequential sub-PRs along Implementation Units before building.
4. **Build.** Run `/lfg` on the item's plan/scope (or implement the sub-PR
   directly). `/lfg` already runs plan→work→simplify→review→commit→push→PR and
   skips browser tests for backend code. Keep code and docs in separate PRs.
5. **Review until clean** (see gate below). Loop `/autoreview` →
   `/thermo-nuclear-code-quality-review` → GitHub/Codex review, applying valid
   fixes, until one round is fully clean or `review_rounds_cap` is hit (then
   surface residuals in the PR body and **stop** at a soft checkpoint).
6. **Merge decision.**
   - *Auto-merge eligible* + review-clean + CI green/absent → `gh pr merge
     --squash`, delete branch.
   - *Human-gated* → post a concise approval request (PR #, what it does, the
     invariant it preserves, the risk, test evidence) and **stop** at the matching
     checkpoint. Do not merge.
7. **Compound.** On item completion (merge, or on reaching a human gate with the
   code review-clean), run `/ce-compound` to capture session/review learnings into
   `docs/solutions/` — in a **separate docs PR** (auto-merge).
8. **Advance.** Update the Progress ledger (commit in the docs PR), then
   `ScheduleWakeup` to continue with the next ready item. If parked at a
   checkpoint, **omit the wakeup** so the loop ends cleanly; the human resumes by
   re-invoking the loop one-liner after approving/merging.

---

## Review-until-clean gate (detail)

Run on each open code PR before it can merge:

1. `/autoreview` on the PR/diff → triage findings (apply valid; record dismissed
   ones with one-line reasons) → commit fixes → push.
2. `/thermo-nuclear-code-quality-review` → same triage/apply/push.
3. GitHub PR reviews: `gh pr view <n> --json reviews,comments` — resolve every
   actionable external (e.g. Codex) finding; reply/resolve threads.
4. **Convergence:** re-run 1–3. The PR is review-clean only when a full round
   yields **no actionable findings from any source**. Cap at `review_rounds_cap`
   rounds; if not converged, write a `## Unresolved Review Findings` section into
   the PR body and stop at a soft checkpoint for human triage.

Dismissals must be justified (false positive, out-of-scope-and-ticketed, or
accepted-risk-with-reason) — never silent.

---

## Human checkpoints (the only places the loop stops for you)

- **CP1 — Merge of a customer-path PR.** Any human-gated PR, review-clean, awaits
  your `merge`/approval. (PR2, PR3/U7, S3, S5.)
- **CP2 — Live deploy.** Activating context-command env vars in the live `.env`
  and `systemctl --user restart dialpad-webhook.service`. Loop prepares the exact
  diff + commands; you run them (or approve the loop to). Gated by plan KTD6
  (don't enable context commands before PR2's async lands).
- **CP3 — Enable auto-send.** Graduating any intent from shadow/draft to actual
  unattended send (S4). Requires the S6 eval to show a low operator-edit rate
  first; you approve per-intent.
- **CP4 — Product decision.** An item needs a brainstorm-level decision the loop
  can't assume (S4 risk tolerance, S5 line→topic mapping). Loop runs
  `/ce-brainstorm` and stops with the questions.

At any checkpoint the loop posts a tight status (what's ready, the risk, what it
needs from you) and ends. You resume by re-invoking the one-liner.

---

## Compounding learnings

- **Per item:** `/ce-compound` the concrete learnings (review patterns, Attio/
  webhook gotchas, invariant proofs) into `docs/solutions/` as a separate docs PR.
- **End of program:** a final full-session `/ce-compound` summarizing the program
  (what shipped, the review themes, the deferred risks) as its own docs PR.

---

## Stop / resume / failure handling

- **Stop conditions:** backlog complete; a human checkpoint; review non-convergence
  after the cap; or any `/lfg` step failing irrecoverably (surface the failure,
  don't loop on a red tree).
- **Resume:** re-invoke the loop one-liner. Orientation (step 1) re-derives state
  from the ledger + git, so resuming is safe and idempotent.
- **Never:** merge a human-gated PR, edit the live `.env`, restart the live
  service, or enable auto-send without the matching checkpoint approval.
- **Safety reminder:** the production pipeline calls Attio via the direct
  `ATTIO_API_KEY` (REST), never the OAuth MCP — keep it that way.

---

## Progress ledger

> The loop updates this each iteration (committed in the docs PR for the item).
> `state` ∈ {todo, planning, building, reviewing, parked@CPx, merged, deployed}.

| Item | State | PR(s) | Notes |
|------|-------|-------|-------|
| S1 (adapters) | merged | #90 | Phase A adapters |
| runbook | merged | #91 | this doc |
| PR2 (async+idempotency) | merged | #92 | ACK-first + SMS idempotency; 3 adversarial rounds + Codex clean. Learnings: docs/solutions/ack-first-webhook-idempotency.md |
| PR3 (un-gate+deploy) | next | — | U7 (un-gate+provenance) ready now; U8 deploy is CP2 |
| S2 (identity resolver) | todo | — | reuses attio_context.py |
| S3 (branching) | todo | — | depends on S2, PR3 |
| S4 (auto-send) | todo | — | depends on S6, S3; CP3 |
| S5 (routing+write-back) | todo | — | depends on PR2 |
| S6 (eval+pulse) | todo | — | independent; pull forward |
```
