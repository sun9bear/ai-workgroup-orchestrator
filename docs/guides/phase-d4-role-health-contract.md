# Phase D4.2 Role Health Contract + Read-Only Dashboard

## Status

Implemented as a D4.2 fake/preflight minimum slice. This phase is **read-only** and does not open real agents, protected business-repository writes, Git mutation, CodeX Automation modification, or MCP mutation tools.

```text
ready_for_real_agent_execution=false
ready_for_protected_business_repository_write=false
mcp_mutation_tools_exposed=false
allow_write=false
allow_real_agents=false
allow_real_adapter_dispatch=false
allow_real_process_execution=false
allow_push=false
allow_merge=false
allow_deploy=false
target_writes_performed=false
codex_automation_modified=false
```

## Scope

D4.2 adds a deterministic role-health contract so the Orchestrator can answer, from existing SQLite state only:

- which role should act next;
- whether each role is healthy, idle, stale, blocked, failed, unknown, disabled, waiting for a human, waiting for a peer, or seeing an empty queue;
- why a role is blocked or idle;
- whether workflow/review/Git-gate state is currently holding progress;
- whether the dashboard may display role cards without performing any repair action.

Implemented surfaces:

- SQLite schema migration `phase_d4_role_health_contract` (`SCHEMA_VERSION=6`).
- Tables:
  - `agent_states`
  - `agent_health_events`
- Python API:
  - `aiwg.role_health.get_role_health_snapshot(...)`
  - `aiwg.role_health.render_role_health_text(...)`
- CLI:
  - `python -m aiwg.cli role-health --config aiwg.yaml --json`
  - `python -m aiwg.cli role-health-snapshot --config aiwg.yaml --json`
- Dashboard/status integration:
  - `aiwg.dashboard.status.get_status_snapshot(...)` now includes top-level `role_health`.
  - `aiwg.dashboard.status.render_status_text(...)` prints a `Role health` section.

D4.2 intentionally absorbs only the role-health concepts from the Tutti reference plan. It does **not** copy Tutti source code, add Tutti dependencies, require tmux, require Rust runtime assumptions, or introduce real agent execution.

## Safety boundary

D4.2 is a read-only observation layer.

It does **not**:

- initialize or migrate a missing target/business database while producing a health snapshot;
- claim tasks;
- release stale claims;
- update heartbeats;
- insert agent-health events from the read-only dashboard path;
- create dashboard repair actions;
- wake CodeX Desktop, Claude Code, OpenCode, or any other real agent;
- create business implementation task cards;
- write into `D:/example/protected-business-repo`;
- expose MCP write/mutation tools.

The role-health snapshot opens the configured SQLite database with URI `mode=ro` and reports:

```json
{
  "read_only": true,
  "mutation_actions": [],
  "ready_for_real_agent_execution": false,
  "ready_for_protected_business_repository_write": false,
  "mcp_mutation_tools_exposed": false,
  "target_writes_performed": false,
  "codex_automation_modified": false
}
```

D4.2 mutable state created by schema migration remains Orchestrator-side only:

```text
<orchestrator_root>/docs/ai-workgroup/state/tasks.sqlite
```

The protected AIVideoTrans repository is used only as read-only boundary context during verification.

## Role contract

D4.2 defines five dashboard role cards:

| Role key | Display name | Purpose |
| --- | --- | --- |
| `tech_lead_planner` | Tech Lead / Planner | Detects planning/scheduling blockage and unconsumed ready work. |
| `reviewer` | Reviewer | Detects pending review work and reviewer blockage. |
| `git_steward` | Git Steward | Detects pending dry-run Git/PR gate work. |
| `claude_implementer` | Claude Implementer | Detects implementation runner state without starting Claude Code. |
| `advisor_runner` | Advisor Runner | Detects advisor/fake-runner queue state without dispatching real agents. |

Health statuses are closed by SQLite CHECK constraints and by the Python contract:

```text
healthy
idle
stale
blocked
failed
unknown
disabled
waiting_human
waiting_peer
queue_empty
```

Health reasons are also closed:

```text
no_recent_heartbeat
ready_task_unconsumed
claimed_task_stale
failed_task_present
human_gate_present
runner_disabled
scheduler_disabled
queue_empty
reviewer_pending
git_steward_pending
recent_heartbeat
```

## SQLite schema

Migration `phase_d4_role_health_contract` adds:

### `agent_states`

```text
role TEXT PRIMARY KEY
display_name TEXT NOT NULL
adapter_type TEXT NOT NULL
enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1))
health_status TEXT NOT NULL CHECK(...closed D4.2 status enum...)
health_reason TEXT CHECK(...closed D4.2 reason enum...)
last_seen_at TEXT
current_task_id TEXT
detail_json TEXT NOT NULL DEFAULT '{}'
updated_at TEXT NOT NULL
```

`enabled` defaults to `0` so adding the contract cannot accidentally enable real runners.

### `agent_health_events`

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
role TEXT NOT NULL
event_type TEXT NOT NULL
health_status TEXT NOT NULL CHECK(...closed D4.2 status enum...)
health_reason TEXT CHECK(...closed D4.2 reason enum...)
task_id TEXT
payload_json TEXT NOT NULL DEFAULT '{}'
created_at TEXT NOT NULL
```

Indexes:

```text
idx_agent_states_health
idx_agent_health_events_role_created
```

## Evaluation behavior

`get_role_health_snapshot(...)` reads existing control-plane state and emits:

- `roles`: one record for each D4.2 role contract;
- `blockers`: machine-readable role/reason/next-action entries;
- `queue_observations`: counts for ready, stale-ready, claimed, stale-claimed, failed, and human-gated tasks;
- `review_observations`: pending review count;
- `workflow_observations`: pending workflow count and grouped statuses;
- `git_gate_observations`: pending Git-gate count and grouped gate states;
- `current_blocking_classification`: high-level classification such as `mechanism_or_role_blocked`, `queue_empty`, or `clear`;
- `dashboard.cards`: read-only display data;
- `dashboard.auto_repair_actions=[]`.

Current default thresholds:

```text
heartbeat_stale_seconds=1800
ready_task_stale_seconds=3600
claimed_task_stale_seconds=1800
```

## CLI examples

Read role-health JSON:

```bash
python -m aiwg.cli role-health --config aiwg.yaml --json
```

Read the same snapshot via the alias command:

```bash
python -m aiwg.cli role-health-snapshot --config aiwg.yaml --json
```

Render a text view:

```bash
python -m aiwg.cli role-health --config aiwg.yaml
```

The commands are status/dashboard commands only. They do not accept task-claim, task-update, repair, dispatch, write, push, merge, or deploy flags.

## Dashboard integration

`get_status_snapshot(...)` now carries a top-level `role_health` key. The dashboard consumes this as read-only status data.

D4.2 deliberately does not add an auto-fix button or workflow mutation endpoint. If a role is blocked, the dashboard shows the `next_action_role` and reason; it does not perform the action.

## MCP boundary

D4.2 does not add any MCP tools. MCP remains read-only with the existing four tools:

```text
status
list_tasks
get_task
recent_events
```

Forbidden MCP mutation surfaces remain absent:

```text
claim_task
update_task
approve
write
dispatch
start_real_agent
repair
commit
push
merge
deploy
```

## Tests

D4.2 targeted tests are in:

```text
tests/aiwg/dashboard/test_d42_role_health_contract.py
```

They cover:

- schema migration `SCHEMA_VERSION=6` installs `agent_states` and `agent_health_events`;
- `health_status` and `health_reason` are closed by CHECK constraints;
- the role-health evaluator reports ready-task, stale-claim, failed-task, human-gate, review, Git-gate, disabled-runner, heartbeat, and queue-empty conditions;
- CLI `role-health` and `role-health-snapshot` are read-only and emit JSON/text views;
- dashboard/status snapshot includes `role_health`;
- role-health snapshot generation does not change the SQLite digest and does not write into the protected business repository.

## Latest verification

```text
D4.2 initial RED: expected failure because aiwg.role_health did not exist yet
D4.2 targeted after D4.2.1: 8 passed in 0.74s
D4.2 full suite after D4.2.1: 259 passed in 46.39s
doctor: AIWG doctor: OK; non-blocking warning remains because orchestrator is not a git repository
CLI role-health: read_only=true; mutation_actions=[]; current_blocking_classification=mechanism_or_role_blocked
CLI role-health-snapshot: read_only=true; mutation_actions=[]; current_blocking_classification=mechanism_or_role_blocked
MCP tools: status,list_tasks,get_task,recent_events only; tool_count=4
AIVideoTrans D4.2 boundary scan: phase-d4-role-health files=0; role_health/role-health files=0; *.sqlite files=0; write_gate/write-gate files=0; git-plan/pr-gate artifacts=0; D4.2 role-health content markers=0
```

The AIVideoTrans repository has pre-existing `docs/ai-workgroup/state` logs and pre-existing normal application files whose names include `workflow`. D4.2 did not create `docs/ai-workgroup/state/artifacts`, `docs/ai-workgroup/state/artifacts/phase-d4-role-health`, SQLite files, role-health files, write-gate files, or Git/PR gate artifacts inside the business repository.

## Independent review

A first broad independent review attempt timed out before returning a verdict, so it was not used as evidence. A second lightweight independent review then completed after the guide and acceptance artifact were written.

```text
passed=true
security_concerns=[]
logic_errors=[]
safety_issues=[]
docs_issues=[]
reviewer targeted: 7 passed in 0.70s
reviewer MCP: status,list_tasks,get_task,recent_events only
```

Reviewer summary:

```text
D4.2 角色健康与 dashboard 复核通过：实现和文档保持只读边界，未发现 real-agent、protected-write 或 MCP mutation 风险。
```

D4.2 is marked `completed_ready_for_codex_review` in the acceptance artifact.

## D4.2.1 CodeX P2 semantic hotfix

CodeX conditionally passed D4.2 and found one P2 semantic issue: when `agent_states` already contained a non-`healthy` `health_status` and a valid `health_reason`, the dashboard could override that persisted reason with `no_recent_heartbeat` because the fallback expression only considered the stored reason on the `healthy` branch.

D4.2.1 fixes the agent-state reason inheritance logic:

- If `agent_state.health_reason` exists and belongs to `ROLE_HEALTH_REASONS`, preserve it.
- If `status == healthy` and no reason exists, default to `recent_heartbeat`.
- If a non-healthy status has no reason, default to `no_recent_heartbeat`.
- If a reason is invalid, downgrade with the existing fallback behavior instead of trusting it.

Regression coverage added:

```text
tests/aiwg/dashboard/test_d42_role_health_contract.py::test_role_health_preserves_valid_agent_state_reasons_and_downgrades_invalid_reason
```

Cases covered:

```text
blocked + scheduler_disabled -> scheduler_disabled
waiting_peer + git_steward_pending -> git_steward_pending
invalid reason -> downgraded fallback, not trusted
```

D4.2.1 verification:

```text
RED before fix: blocked + scheduler_disabled was incorrectly reported as no_recent_heartbeat
single regression after fix: 1 passed in 0.14s
D4.2 targeted: 8 passed in 0.74s
full suite: 259 passed in 46.39s
doctor: AIWG doctor: OK
MCP tools: status,list_tasks,get_task,recent_events only
CLI role-health smoke: read_only=True; mutation_actions=[]; mcp_mutation_tools_exposed=False
lightweight independent review: passed=true; hotfix regression 1 passed in 0.08s; security_concerns=[]; logic_errors=[]; safety_issues=[]; docs_issues=[]
```

No safety boundary changed in D4.2.1: real agents, protected writes, MCP mutation tools, dashboard auto-repair, CodeX Automation modification, Git push/merge/deploy all remain disabled.

## Remaining constraints before later phases

D4.2 is **not** approval to execute real agents or protected writes. Later phases must still keep defaults fail-closed unless the user explicitly authorizes real execution:

```text
allow_write=false
allow_real_agents=false
allow_real_adapter_dispatch=false
allow_real_process_execution=false
allow_push=false
allow_merge=false
allow_deploy=false
allow_modify_codex_automations=false
mcp_mutation_tools_exposed=false
ready_for_real_agent_execution=false
ready_for_protected_business_repository_write=false
```
