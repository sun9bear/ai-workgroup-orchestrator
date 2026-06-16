# Phase D4.3 External Review Gate Read-Only

## Status

D4.3 已实现为 fake/preflight 方向的只读状态汇总层，并通过 D4.3.1 完成验收收口。它把 GitHub PR review、CodeX review report、Reviewer agent report、Human report、CI / future scanner 等外部审核来源归一成 `external_review_gate` snapshot，但 **review gate 是状态，不是动作**。

D4.3.1 closure 修复了 CodeX P1，并完成 malformed/non-list `mutation_actions_json` post-fix 收口：`acceptance.json` 已刷新到最新 `9 passed` targeted / `268 passed` full-suite 证据；source safety guard 会在 legacy/dirty source row 声明 `read_only=0`、有效非空 list、有效非 list JSON、或 malformed 非空 `mutation_actions_json` 时，只读地发出 `safety_warnings` 并将状态保守置为 `blocked`。

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
git_push_performed=false
git_merge_performed=false
pr_comment_performed=false
target_writes_performed=false
codex_automation_modified=false
```

## Scope

D4.3 只吸收 `docs/plans/2026-06-06-aiwg-tutti-reference-absorption-plan.md` 中 `generic external review gate` 的概念边界：

- review gate 是状态机，不是自动修复器；
- external review source 是抽象结构，不写死 GitHub；
- 统一 review gate 状态；
- 统一 actionable feedback 分类；
- 只读汇总，不 push、不 merge、不评论 PR、不创建修复任务。

D4.3 没有复制 Tutti 源码，没有引入 Tutti 依赖，没有引入 tmux/Rust runtime 假设，也没有启用 real agent execution。

## Implemented surfaces

- SQLite schema migration `phase_d4_external_review_gate` (`SCHEMA_VERSION=7`).
- Tables:
  - `external_review_sources`
  - `external_review_items`
  - `external_review_gate_snapshots`
- Python API:
  - `aiwg.external_review_gate.get_external_review_gate_snapshot(...)`
  - `aiwg.external_review_gate.classify_external_review_items(...)`
  - `aiwg.external_review_gate.render_external_review_gate_text(...)`
- CLI:
  - `python -m aiwg.cli external-review-gate --config aiwg.yaml --json`
  - `python -m aiwg.cli external-review-gate --config aiwg.yaml`
- Dashboard/status integration:
  - `aiwg.dashboard.status.get_status_snapshot(...)` includes top-level `external_review_gate`.
  - `aiwg.dashboard.status.render_status_text(...)` prints an `External review gate` section.

## Safety boundary

D4.3 是只读 diagnosis / summary 层。它不会：

- 调用 GitHub write API；
- poll GitHub API in the first implementation slice；
- push、merge、commit、deploy；
- comment PR；
- create Claude-Code / CodeX / Reviewer 修复任务；
- claim tasks；
- mutate review rows while reading snapshots；
- 写入 `D:/example/protected-business-repo`；
- 修改 CodeX Automations；
- 暴露 MCP mutation tools。

`get_external_review_gate_snapshot(...)` 对已存在 SQLite 使用 URI `mode=ro` 打开，snapshot 显式返回：

```json
{
  "read_only": true,
  "mutation_actions": [],
  "github_write_api_called": false,
  "git_push_performed": false,
  "git_merge_performed": false,
  "pr_comment_performed": false,
  "created_fix_tasks": false,
  "target_writes_performed": false,
  "ready_for_real_agent_execution": false,
  "ready_for_protected_business_repository_write": false,
  "mcp_mutation_tools_exposed": false,
  "codex_automation_modified": false
}
```

If legacy or dirty `external_review_sources` rows claim write capability, D4.3.1 treats that as a safety warning and a blocking gate condition while preserving the read-only top-level contract:

```text
read_only=0 -> safety_warnings[].code=external_review_source_not_read_only; gate_state=blocked
mutation_actions_json valid non-empty list -> safety_warnings[].code=external_review_source_mutation_actions_present; gate_state=blocked
mutation_actions_json valid non-list JSON or malformed non-empty string -> safety_warnings[].code=external_review_source_mutation_actions_present; gate_state=blocked
snapshot.read_only=true
snapshot.mutation_actions=[]
```

This guard is diagnostic only. It does not rewrite the dirty row, persist a snapshot row, call GitHub APIs, comment PRs, create tasks, or perform any repository mutation.

Mutable schema/state remains under the Orchestrator root only:

```text
<orchestrator_root>/docs/ai-workgroup/state/tasks.sqlite
```

The protected AIVideoTrans repository is used only as read-only boundary context during verification.

## External review source contract

`external_review_sources` abstracts external review systems. Current closed source type enum:

```text
github_pr
codex_report
reviewer_report
human_report
ci
coderabbit
security_scanner
other
```

Each source stores:

```text
id TEXT PRIMARY KEY
source_type TEXT CHECK(...closed D4.3 source enum...)
display_name TEXT NOT NULL
provider_ref TEXT
gate_state TEXT CHECK(...closed D4.3 gate enum...)
last_polled_at TEXT
read_only INTEGER DEFAULT 1 CHECK(read_only IN (0, 1))
mutation_actions_json TEXT DEFAULT '[]'
payload_json TEXT DEFAULT '{}'
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

## Review gate statuses

D4.3 gate states are closed by SQLite CHECK constraints and the Python API:

```text
not_polled
no_pr
pending_review
approved
changes_requested
blocked
ci_failed
stale
unknown
```

Classification priority is conservative:

1. missing DB -> `not_polled`;
2. no sources and no items -> `no_pr`;
3. human gate or source `blocked` -> `blocked`;
4. source `ci_failed` -> `ci_failed`;
5. unresolved `must_fix` / blocking feedback or source `changes_requested` -> `changes_requested`;
6. source `pending_review` or unresolved `question` -> `pending_review`;
7. stale source -> `stale`;
8. only approved/no_pr source states -> `approved`;
9. otherwise -> `unknown`.

## Actionable feedback contract

`external_review_items` normalizes individual findings from review reports:

```text
id TEXT PRIMARY KEY
source_id TEXT NOT NULL REFERENCES external_review_sources(id)
source_type TEXT CHECK(...closed D4.3 source enum...)
item_state TEXT DEFAULT 'open' CHECK(item_state IN ('open', 'resolved', 'dismissed', 'stale'))
feedback_category TEXT CHECK(...closed D4.3 feedback enum...)
title TEXT NOT NULL
body TEXT DEFAULT ''
file_path TEXT
line INTEGER CHECK(line IS NULL OR line >= 1)
resolved INTEGER DEFAULT 0 CHECK(resolved IN (0, 1))
blocking INTEGER DEFAULT 0 CHECK(blocking IN (0, 1))
payload_json TEXT DEFAULT '{}'
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

Feedback categories are closed:

```text
must_fix
should_fix
question
non_blocking
human_gate
out_of_scope
```

Actionable unresolved feedback includes:

```text
must_fix
should_fix
question
human_gate
```

`non_blocking` and `out_of_scope` remain visible in counts but are not treated as actionable blockers. Rendered actionable feedback is sorted by severity (`human_gate`, `must_fix`, `should_fix`, `question`) and then timestamp/id for deterministic output.

## Snapshot registry table

`external_review_gate_snapshots` is a future-proof Orchestrator-side registry for persisted diagnosis summaries. D4.3 read paths can read it, but the first implementation does not write snapshots from the CLI/dashboard path.

```text
id TEXT PRIMARY KEY
gate_state TEXT CHECK(...closed D4.3 gate enum...)
source_count INTEGER CHECK(source_count >= 0)
item_count INTEGER CHECK(item_count >= 0)
unresolved_actionable_count INTEGER CHECK(unresolved_actionable_count >= 0)
summary_json TEXT DEFAULT '{}'
read_only INTEGER DEFAULT 1 CHECK(read_only IN (0, 1))
mutation_actions_json TEXT DEFAULT '[]'
git_push_performed INTEGER DEFAULT 0 CHECK(git_push_performed IN (0, 1))
git_merge_performed INTEGER DEFAULT 0 CHECK(git_merge_performed IN (0, 1))
pr_comment_performed INTEGER DEFAULT 0 CHECK(pr_comment_performed IN (0, 1))
target_writes_performed INTEGER DEFAULT 0 CHECK(target_writes_performed IN (0, 1))
created_at TEXT NOT NULL
```

Indexes:

```text
idx_external_review_sources_state
idx_external_review_items_source_category
idx_external_review_gate_snapshots_created
```

## CLI examples

Read JSON snapshot:

```bash
python -m aiwg.cli external-review-gate --config aiwg.yaml --json
```

Read text view:

```bash
python -m aiwg.cli external-review-gate --config aiwg.yaml
```

The command is a status/dashboard command only. It does not accept repair, task creation, PR comment, push, merge, deploy, or GitHub write flags.

## Dashboard integration

`get_status_snapshot(...)` carries top-level `external_review_gate`. The text dashboard includes an `External review gate` section, for example:

```text
External review gate
- status=no_pr | sources=0 | items=0 | unresolved_actionable=0 | pr_comment_performed=false
```

This is display data only; no auto-fix button or mutation action is exposed.

## MCP boundary

D4.3 does not add any MCP tools. MCP remains read-only with the existing four tools:

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
comment_pr
commit
push
merge
deploy
```

## Tests

D4.3 targeted tests are in:

```text
tests/aiwg/review/test_d43_external_review_gate.py
```

They cover:

- schema migration `SCHEMA_VERSION=7` installs `external_review_sources`, `external_review_items`, and `external_review_gate_snapshots`;
- source type, gate state, and feedback category are enforced by SQLite CHECK constraints;
- generic source abstraction across `github_pr`, `codex_report`, `reviewer_report`, `human_report`, and `ci`;
- status classification for `approved`, `pending_review`, `changes_requested`, `blocked`, `ci_failed`, `stale`, `no_pr`, and `not_polled` cases;
- actionable feedback classification and severity ordering;
- `get_external_review_gate_snapshot(...)` does not change the SQLite digest;
- D4.3.1 source safety guard blocks and warns on legacy rows with `read_only=0`, valid non-empty list `mutation_actions_json`, valid non-list JSON, or malformed non-empty `mutation_actions_json` while leaving the DB digest unchanged;
- CLI JSON/text smoke paths are read-only;
- dashboard/status snapshot includes `external_review_gate`;
- target/business repository boundary remains untouched.

## Latest verification

```text
D4.3 initial RED: expected collection failure before implementation because aiwg.external_review_gate did not exist.
D4.3.1 safety guard RED: python -m pytest tests/aiwg/review/test_d43_external_review_gate.py::test_external_review_gate_blocks_and_warns_on_write_capable_source_rows -q -> failed as expected before implementation because an approved dirty source row was not blocked.
D4.3.1 safety guard GREEN: python -m pytest tests/aiwg/review/test_d43_external_review_gate.py::test_external_review_gate_blocks_and_warns_on_write_capable_source_rows -q -> 1 passed in 0.11s
D4.3.1 malformed/non-list guard RED: python -m pytest tests/aiwg/review/test_d43_external_review_gate.py::test_external_review_gate_blocks_non_array_or_malformed_mutation_actions_json -q -> 3 failed as expected before fix because malformed/non-list mutation_actions_json rows were still approved.
D4.3.1 malformed/non-list guard GREEN: python -m pytest tests/aiwg/review/test_d43_external_review_gate.py::test_external_review_gate_blocks_non_array_or_malformed_mutation_actions_json -q -> 3 passed in 0.19s
D4.3 targeted: python -m pytest tests/aiwg/review/test_d43_external_review_gate.py -q -> 9 passed in 0.96s
D4.3 related regression slice: python -m pytest tests/aiwg/review/test_d43_external_review_gate.py tests/aiwg/dashboard/test_d42_role_health_contract.py tests/aiwg/state/test_a2_sqlite_import.py tests/aiwg/state/test_b0_schema_hardening.py tests/aiwg/runners/test_b7_operator_preflight_approval.py tests/aiwg/git/test_d4_git_steward_dry_run.py -q -> 55 passed in 6.00s
Full suite: python -m pytest -q -> 268 passed in 49.03s
Doctor: python -m aiwg.cli doctor --config aiwg.yaml --project-root . -> AIWG doctor: OK; all mutation/real-agent safety switches remain false; non-blocking warning remains because orchestrator is not a git repository.
MCP tools: python -m aiwg.mcp.server --list-tools -> status,list_tasks,get_task,recent_events only; tool_count=4.
CLI external-review-gate JSON: python -m aiwg.cli external-review-gate --config aiwg.yaml --json -> exit 0; gate_state=no_pr; read_only=true; mutation_actions=[]; safety_warning_count=0; pr_comment_performed=false; target_writes_performed=false; ready_for_real_agent_execution=false; ready_for_protected_business_repository_write=false; mcp_mutation_tools_exposed=false.
Dashboard status paths are covered by the D4.3 targeted suite and remain read-only.
Guide/acceptance secret scan: 0 matches for credential assignment patterns.
AIVideoTrans D4.3 boundary scan: phase-d4-external-review-gate files=0; external-review-gate files=0; *external_review_gate* files=0; *.sqlite files=0; *.db files=0; external_review_sources/items/gate_snapshots markers=0; D4.3/source-safety-guard/malformed-non-list content markers=0.
```

AIVideoTrans has pre-existing historical `external-review` workgroup messages from earlier APF collaboration. D4.3 did not create phase-specific guide/artifact files, SQLite files, external_review_gate markers, or new review-gate artifacts inside the business repository.

## Independent review

Post-fix independent review passed and has been written back to `acceptance.json`.

```text
status=passed
passed=true
reviewed_at=2026-06-07T11:44:30+08:00
targeted_tests=python -m pytest tests/aiwg/review/test_d43_external_review_gate.py -q with bytecode/cache writes disabled -> 9 passed in 1.38s during independent review
doctor=AIWG doctor: OK; expected non-blocking not-a-git-repository warning only
mcp_surface=status,list_tasks,get_task,recent_events only; tool_count=4; no mutation tools
acceptance_json=valid JSON
cli_external_review_gate_json=read_only=true; mutation_actions=[]; gate_state=no_pr; mutation/real-agent/protected-write flags false
security_concerns=[]
logic_errors=[]
safety_issues=[]
docs_issues=[]
blocking_issues=[]
```

Reviewer summary: malformed/non-list `mutation_actions_json` cases are explicitly covered (`"comment_pr"`, `{"action":"comment_pr"}`, and raw `comment_pr`); dirty source rows produce `safety_warnings` and `gate_state=blocked` while preserving top-level `read_only=true` / `mutation_actions=[]`; DB digest remains unchanged before/after snapshot reads.

## Remaining constraints before later phases

D4.3 is **not** approval to poll GitHub, comment on PRs, create fix tasks, execute real agents, or write protected repositories. A later phase may add a GitHub read-only adapter, but it must start behind a separate read-only adapter contract and preserve these defaults unless the user explicitly authorizes a mutation phase:

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
