# Delegate Agent Protocol

## Runtime layout

State defaults to the platform user-state directory and can be overridden with `DELEGATE_AGENT_HOME`.

```text
<state-root>/
├── tasks/<workspace-id>/<timestamp>-<slug>-<agent-id>/
│   ├── metadata.json
│   ├── mailbox.db
│   ├── backend-events.jsonl
│   ├── stderr.log
│   ├── runner.log
│   └── final.txt
├── groups/<group-id>/
│   ├── metadata.json
│   └── events.db
└── workspace-locks/<workspace-id>.json
```

Each run owns its database and process tree. Its backend-local MCP server communicates only through that private database. The group database stores membership and an aggregate event cursor; it does not replace worker mailboxes.

## Lifecycle

```text
created → starting → running → completed
                       ├──────→ waiting_for_parent → running
                       ├──────→ cancelling → cancelled
                       └──────→ failed/interrupted
```

## Parent CLI

All successful commands print one JSON object to stdout. Errors also print JSON and exit with status 2.

```text
doctor
backends
backend BACKEND
models --backend BACKEND [--include-hidden]
capabilities --backend BACKEND [--include-hidden]
roles
role ROLE_ID
resolve [--role ROLE] [--complexity LEVEL] [--model MODEL] [--model-policy POLICY] [--reasoning EFFORT] [--sandbox MODE]
spawn --backend BACKEND [--group GROUP_ID] --cwd DIR [--role ROLE] [--complexity simple|standard|complex] [--model MODEL] [--model-policy economy|default|capable] [--reasoning EFFORT] --prompt TEXT
group create [--name NAME]
group list
group status GROUP_ID
group respond GROUP_ID REQUEST_ID ANSWER
supervise GROUP_ID [--after CURSOR] [--limit N] [--timeout SECONDS] [--until CONDITIONS] [--follow]
list [--active] [--workspace DIR]
status AGENT_ID
events AGENT_ID [--after CURSOR] [--limit N] [--wait SECONDS]
wait AGENT_ID [--timeout SECONDS]
steer AGENT_ID TEXT [--priority normal|urgent] [--interrupt]
respond AGENT_ID REQUEST_ID ANSWER
cancel AGENT_ID
resume AGENT_ID --prompt TEXT [--model MODEL] [--reasoning EFFORT]
result AGENT_ID
cleanup
```

`spawn` creates a group when `--group` is omitted. `supervise` defaults to `--until action,failed,warning,complete`; `reason` is `action_required`, `worker_failed`, `supervision_warning`, `complete`, or `timeout`. Progress events do not end the wait early. `--follow` removes the timeout and keeps the command attached until a selected condition occurs; it does not resurrect a caller process that has already exited. `group respond` routes a response to the private mailbox that owns the request. A group completes only when every worker is terminal and no parent response is pending. Direct worker-to-worker communication is intentionally unsupported.

Message types have different supervision semantics. `input.requested` is actionable worker-to-coordinator communication: it appears in `coordinator_inbox` and wakes with `reason: action_required`. `agent.message` is informational output and `progress.reported` is a milestone; both are returned in `events` but do not independently wake supervision. A final `agent.message` normally accompanies completion, which wakes through the `complete` condition. Coordinators must persist `next_cursor`, process all returned events, and use it as the next `--after` value. When `truncated` is true, fetch again immediately before beginning another wait.

Group events are compact and coordinator-relevant: progress, parent requests and responses, final messages, mailbox failures, warnings, results, and worker lifecycle events. Raw backend and tool events remain in each run's `backend-events.jsonl` and per-run `events` command; they are not copied into the group database.

`supervise --limit N` never advances past omitted events. It returns at most `N`, leaves `next_cursor` at the last returned event, and sets `truncated: true` when more remain. Group-mirroring failures appear as worker `group_mirror_error` warnings and force `action_required` because aggregate supervision may be incomplete.

Checkpoint steering reports `delivery: checkpoint` for active workers and `delivery: next_resume` for terminal workers. The latter remains queued in the private mailbox until resume. `--interrupt` rejects terminal workers instead of appearing to succeed.

Spawn registration is transactional: a failure before the detached launcher starts removes the member, run directory, workspace lock, and automatically-created empty group. `cleanup` removes retained runs according to their normal retention and then prunes missing members and empty groups.

`models` queries the installed Codex app-server `model/list` catalog. It returns only models available to the current Codex installation/account, including the default model, supported reasoning efforts, modalities, service tiers, and upgrade hints. Never hard-code this catalog.

## Role resolution

Roles are optional and default to `general`. Bundled definitions live under `roles/`; valid user overrides under `<state-root>/roles/` replace a bundled role with the same ID.

```text
explicit --model
→ explicit --model-policy
→ role + selected complexity policy
→ catalog default
```

Reasoning resolves from explicit `--reasoning`, then the role-complexity profile, then a supported model fallback. Explicit unsupported models or reasoning efforts fail instead of silently changing the caller's request.

- `economy`: prefer the canonical Luna model, then catalog default.
- `default`: use catalog default.
- `capable`: prefer the canonical Terra model, then Luna, then catalog default.

Spawned Codex agents receive `agents.enabled=false` to avoid uncontrolled nested-agent expansion.

The injected `delegate_parent` MCP server is private to one run and exposes only the six bundled mailbox tools. The runner sets `default_tools_approval_mode="approve"` for this server so non-interactive `approval_policy="never"` sessions do not auto-reject checkpoint, progress, or completion calls. Do not apply this approval mode to unrelated MCP servers.

## Codex-local MCP tools

- `check_inbox`: read pending parent instructions.
- `acknowledge`: mark instructions incorporated.
- `report_progress`: publish a semantic milestone.
- `request_input`: create a blocking parent question.
- `wait_for_response`: wait up to 30 seconds for its answer.
- `complete`: record structured files, tests, warnings, and summary.

MCP does not inject unsolicited tokens into an active model response. Normal steering is observed at the next checkpoint. Interrupt steering stops the active `codex exec` process group and resumes its persisted session.

## Concurrency

- Every Codex agent has a random ID and separate mailbox.
- Multiple read-only agents may share a checkout.
- One write-capable agent may use a checkout at a time.
- Separate Git worktrees permit parallel write-capable agents.
- `--allow-shared-checkout` disables the write lock and is intentionally explicit.

## Environment

- `DELEGATE_AGENT_HOME`: state root override.
- `DELEGATE_AGENT_CODEX_BIN`: alternate Codex executable, useful for testing.
- `DELEGATE_AGENT_CONTROLLER`: default caller identity.
- `DELEGATE_AGENT_ALLOW_DANGER=1`: prerequisite for explicitly requested `danger-full-access`.
