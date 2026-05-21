---
status: active
created: 2026-05-21
type: fix
---

# Dialpad Lines Transcript Parsing and Runtime Sync

## Problem

Dialpad call `5479428721549312` returned transcript data from `GET /api/v2/transcripts/{call_id}` as a `lines` array, but the transcript wrapper only parsed string transcript fields and list fields named `utterances`, `segments`, `items`, or `transcript`. The wrapper therefore reported `available: false` and `unavailable_reason: no_transcript` even though Dialpad had transcript lines.

Runtime skill copies can also drift from the source tree. The existing workspace sync command at `~/projects/skills/sync-skills.sh` is the correct deployment path because it materializes project skills into OpenClaw/AlphaClaw skill roots as real directories.

## Scope Boundaries

- Fix transcript parsing in this skill source tree.
- Add regression coverage for Dialpad's `lines` response shape.
- Run the existing workspace sync script after the source fix is verified.
- Install a user systemd timer that runs the existing sync script nightly.
- Do not rewrite the generated Dialpad OpenAPI CLI.
- Do not change Attio note behavior in this patch.
- Do not replace `sync-skills.sh` with ad hoc runtime-copy logic.

## Existing Patterns

- `scripts/get_transcript.py` owns response normalization for transcript payloads.
- `tests/test_get_transcript.py` covers parser response shapes and unavailable states.
- `bin/get_call_transcript.py` is the agent-facing wrapper and should remain transcript-only.
- `~/projects/skills/sync-skills.sh` already syncs project skill source directories into `~/.openclaw/skills` and AlphaClaw's materialized skill root when present.

## Implementation Units

### U1: Parse Dialpad `lines` Transcript Payloads

**Files**

- Modify: `scripts/get_transcript.py`
- Test: `tests/test_get_transcript.py`

**Approach**

Extend `format_transcript()` to treat `lines` as a list candidate, include `name` as a speaker field, and ignore non-transcript line types such as moments so summary fragments do not become transcript text.

**Test Scenarios**

- A payload with `lines` containing `type: transcript`, `name`, and `content` renders speaker-prefixed transcript text.
- A non-transcript line in the same payload is skipped.
- Existing payload shapes still pass unchanged.

**Verification**

- `python3 -m pytest tests/test_get_transcript.py tests/test_get_call_transcript_wrapper.py`
- Live smoke command for the known call returns `available: true` from `bin/get_call_transcript.py --call-id 5479428721549312 --json`.

### U2: Keep Runtime Skills Synchronized Nightly

**Files**

- Create live user systemd files under `~/.config/systemd/user/`.

**Approach**

Install a user service that executes `/home/art/projects/skills/sync-skills.sh` from `/home/art/projects/skills`, and a timer that runs nightly with persistent catch-up. Enable and start the timer with `systemctl --user`.

**Test Scenarios**

- `systemd-analyze verify --user` accepts the service and timer files.
- `systemctl --user list-timers` shows the nightly sync timer after enablement.
- A manual service start succeeds or surfaces a concrete sync failure.

**Verification**

- `~/projects/skills/sync-skills.sh`
- `systemctl --user daemon-reload`
- `systemctl --user enable --now skills-sync.timer`
- `systemctl --user start skills-sync.service`
- `systemctl --user status skills-sync.service --no-pager`
- `systemctl --user list-timers skills-sync.timer --no-pager`

## Risks

- `sync-skills.sh` prunes managed skill roots to match project sources. This is intended behavior, but verification should confirm the Dialpad runtime copy is present after sync.
- The workspace root is not a git repository, so live systemd files are runtime configuration rather than part of this skill PR. The code/test portion remains durable in this skill repository.

## Final Verification Checklist

- Focused parser tests pass.
- The known Dialpad call now returns transcript text.
- Runtime skill copies match the source parser after `sync-skills.sh`.
- The nightly systemd timer is enabled and scheduled.
