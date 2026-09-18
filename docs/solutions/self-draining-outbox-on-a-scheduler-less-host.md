---
module: scripts/log_outbox.py
problem_type: concurrency, data-loss, observability, alerting, fleet-drift
tags: [outbox, self-drain, flock, identity-compaction, quarantine, alert-dedupe, grokbot, scheduler-less]
related_pr: ""
---

# A queue nobody drains is a queue nobody sees

Grok Bot queued 18 outbound SMS observations over three days while every one of
them had actually reached the shared log within two seconds via the webhook. The
outbox was never a delivery buffer here; it was a second, wrong, copy of the
truth. Draining it by hand took one command and lost nothing.

The queueing cause was already fixed — PR #153 auto-loaded `dialpad.env`, and
the last enqueue landed one minute before that merge. What nobody had fixed was
the drain: `python3 scripts/log_outbox.py replay` had exactly one caller, and
that caller was a human.

## The queue was the symptom; the drain was the bug

An outbox whose only trigger is manual is a queue with a lag equal to however
long it takes someone to notice. Nothing errored, because enqueue is designed
to be silent on a confirmed send. The 18 entries were the only evidence, and
they sat under `~/.dialpad/` on a machine nobody logs into.

**Assume a manually-drained queue is not draining.** Measure the backlog depth
before assuming the retry path works.

## Making the drain inline makes the race worse, not better

`replay_outbox` read the whole file, recorded each entry, then wrote the
survivors back. That is loss-free only while nothing appends mid-drain. Draining
at send time *creates* exactly that concurrency: the send that triggers the
drain can enqueue while the drain is in flight.

Two independent things were needed, and only one of them is a lock:

- **Identity-based compaction.** Re-read the live file at commit and drop only
  the identities actually recorded. Whatever appeared since the first read is
  kept, because it was never in the drop set.
- **One exclusive lock** spanning read, drain, and commit, shared with enqueue,
  held on a *sibling* file — the outbox itself gets `os.replace`d, so it cannot
  be the lock.

The lock reduces interleaving; the identity compaction is what makes a lost
append impossible. A lock alone would still lose an entry the moment a caller
proceeded after a lock timeout — and enqueue must be able to proceed, because a
confirmed customer SMS cannot fail because a logging queue is busy.

```python
def _entry_identity(parsed):
    observation = parsed.get("observation") or {}
    return (parsed.get("queued_at"), observation.get("provider_id"))
```

Identity needs no new field. Two lines with the same queue time and provider id
carry the same observation, and recording that twice is already a merge rather
than a duplicate.

## A drain that declined must not report an empty queue

`{"remaining": 0}` from a drain that never ran is worse than an error. The
caller reads it as "caught up" and the queue keeps growing. Lock contention
reports `skipped: 1` alongside the real line count, and every caller treats
non-zero `skipped` as *later*.

Same shape, smaller: a quarantine write that fails **keeps** the poison line in
the active file. Moving a line you cannot write is deleting it.

## A budget on a client that has no retry budget

`log_api_client.py` has a 5s timeout and no retry budget. 18 stale entries
drained inline is ~90 seconds inside a customer-visible send. So the drain is
capped twice over — 5 entries, 2.0s wall clock checked *between* entries — and
runs strictly after the receipt or the read output is committed. The residual
overrun is one in-flight request, and the caps only count as breached once work
was actually possible, so a budget that never mattered is not reported as
missed.

`drain_on_use` never raises, by contract, rather than by each call site
remembering a `try`. That removes the failure at its source instead of
requiring four call sites to each get it right.

## A clock-derived field in a delivery hash re-sends an unchanged report

The dedupe key is `f"{oldest_queued_at}:{depth}"` — deliberately not
`oldest_age_seconds`. Age crosses the 24h boundary on its own while nothing
about the queue changes, and a key containing it re-delivers an identical
operator alert. Anything else about the record may tick freely.

The rest of the alert discipline is the fleet rule, unchanged: write the marker
undelivered *before* attempting, flip to delivered only on a strictly `is True`
confirmation (a preview, a stub, or a truthy-but-unconfirmed return never
flips it), clear it when depth reaches zero so the next breach is a fresh
breach, and treat an unreadable marker as undelivered. Fail toward noise: a
duplicate alert is survivable, a breach that quietly suppresses itself is not.

## Do not index the backlog

Depth and oldest age are derived from the outbox at run time. A maintained
"first seen" record is a second source of truth about what is waiting, and
whenever the two disagree it takes a repair tool to say which is lying. The
outbox is the only thing allowed to know what is pending.

The one piece of maintained state is the run record — one append line per drain
carrying its own timestamp. It decides nothing. It exists because **nothing on
that host can notice its own silence**: no cron, no systemd user timer, so a
host that is simply never used produces nothing. The only honest answer is a
timestamp an outside clock-reader can inspect, plus stating the limit instead
of claiming it away.

## Delivery: a named set, never a recursive copy

Three copies of this skill run, and none is uniformly ahead: the repo has scrub
fixes the gateway copy lacks, the gateway copy has redaction fixes grokbot
lacks, and grokbot has PR #153 while the gateway copy does not. Copying a tree
in either direction ships one copy's staleness into another.

Delivery is a reviewed manifest plus a content-hash probe that reports only
differing files, run before and after applying. The manifest is
`references/outbox-runtime-files.txt`, and the probe has no mode that walks a
tree.

## Process notes

- **`str.replace` silently no-ops.** Two patches in this work "applied" while
  changing nothing, and one test passed for the wrong reason for several steps
  before a full-suite run exposed it. Assert the needle is present, and assert
  the result.
- **Do not invent a seam to test against.** A test was written against a
  `_finish_send` function that did not exist and does not need to. The real fix
  was to make the drain non-raising at its source, which is smaller, and gets
  every caller the guarantee for free.
- **Same-stem modules bite at suite scope.** `bin/list_calls.py` and
  `scripts/list_calls.py` share a name; a test importing by name resolved
  whichever directory an earlier test left on `sys.path`. Load by explicit path
  under a private alias, as the wrappers themselves already do, and assert the
  loaded file is the one you meant.
