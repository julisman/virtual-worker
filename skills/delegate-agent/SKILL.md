---
name: delegate-agent
description: Spawn, discover, monitor, steer, cancel, and resume durable background CLI coding agents through a backend-neutral interface. Use when delegating repository work to Codex today or when implementing another backend such as Claude Code, while retaining model selection, progress inspection, mailbox steering, concurrent isolated runs, and follow-up turns without a daemon.
---

# Delegate Agent

Use `scripts/agent.py`. Both the Codex and Claude Code backends are implemented; each reports `usable: true` when its CLI is installed. Always confirm with `backends` before spawning.

## Discover

```bash
python3 <skill-dir>/scripts/agent.py backends
python3 <skill-dir>/scripts/agent.py backend codex
python3 <skill-dir>/scripts/agent.py models --backend codex
```

Never assume a backend is installed or usable. Pass `--backend` explicitly when spawning.

## Spawn

```bash
python3 <skill-dir>/scripts/agent.py spawn --backend codex --cwd <repo> --role <role> --complexity <level> --prompt "<task>"
```

Retain the returned `agent_id`. Use `workspace-write` for implementation and `read-only` for inspection. Full access requires explicit authorization and `DELEGATE_AGENT_ALLOW_DANGER=1`.

Every spawn belongs to a group. Omit `--group` to create one automatically, or pass a group returned by `group create --name "<workflow>"`. Use the same group for related Codex and Claude workers. Workers retain private mailboxes; the group stores aggregate events and coordinator routing metadata.

## Supervise

- Prefer `supervise <group-id> --after <cursor> --timeout 30` for multiple workers. By default it wakes for parent action, worker failure/interruption, supervision warnings, or group completion; inspect `reason`, `timed_out`, `truncated`, and `next_cursor`.
- Use `--until action,failed,warning,complete` to select wake conditions. Use `--follow` to wait without a timeout when the caller can keep one shell command attached. When truncated, call again with the returned cursor so no events are skipped.
- Inspect `action_required` and answer questions with `group respond <group-id> <request-id> "<answer>"`.
- Use `group status <group-id>` for aggregate state and `group list` for discovery.
- `wait <agent-id> --timeout 30`
- `events <agent-id> --after <cursor>`
- `status <agent-id>`
- `respond <agent-id> <request-id> "<answer>"` when `pending_request` is present

A group is complete only when every worker is terminal and no coordinator response is pending. Direct worker-to-worker communication is intentionally unsupported; the caller forwards findings using `steer`.

### Message semantics

- `input.requested` is worker-to-coordinator communication that requires a response. It wakes supervision with `reason: action_required`; read the request from `coordinator_inbox`, then use `group respond`.
- `agent.message` is informational output, and `progress.reported` is a milestone. Both appear in `events` but do not wake supervision by themselves. A final `agent.message` normally arrives with worker completion, which wakes with `reason: complete`.
- Treat `next_cursor` as durable caller state. Start with `--after 0`, process every returned event, save `next_cursor`, and pass it to the next call. If `truncated: true`, call again immediately with that cursor before waiting for new activity.

Typical coordinator loop:

```text
supervise GROUP --after CURSOR --follow
process events and save next_cursor
if reason == action_required: group respond GROUP REQUEST_ID ANSWER
if reason is complete or worker_failed: inspect result/status
otherwise: supervise again using the saved cursor
```

## Steer and Continue

- `steer <agent-id> "<instruction>"` queues checkpoint steering.
- Add `--interrupt` only for an urgent direction change.
- Steering a terminal worker returns `delivery: next_resume`; the instruction is durable but will not run until resume. Interrupting a terminal worker is rejected.
- `resume <agent-id> --prompt "<follow-up>"` continues the originating backend session.
- `cancel <agent-id>` stops active work.
- `result <agent-id>` returns the final captured message.

Use separate worktrees for parallel write-capable agents. Load `references/protocol.md` when implementing a new backend or diagnosing lifecycle behavior.
