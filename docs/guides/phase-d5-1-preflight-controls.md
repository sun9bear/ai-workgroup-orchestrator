# Phase D5.1 — Preflight controls (fake/dry-run only)

状态：`completed_ready_for_independent_review`
更新时间：`2026-06-07T08:49:33Z`

## 1. 目标

D5.1 在 D5.0 最小 preflight 基础上补齐三个前置控制面能力，但仍严格停留在 fake/dry-run/preflight-only：

1. `--fail-on-blocked`：当 dry-run preflight 得到 `status=blocked` 时，自动化可选择用 exit code `3` fail closed。
2. Budget preflight：记录预算预检，不消费真实预算。
3. Checkpoint lease / heartbeat / stale recovery precheck：只记录“would acquire / would recover”的预检状态，不获取真实锁、不重置任务、不执行 stale recovery。
4. External review fixture ingest：只 ingest 本地 JSON fixture，不调用 GitHub，不评论 PR，不创建 fix task。

## 2. 非目标 / 禁止项

D5.1 不允许以下行为：

- real agent execution：`ready_for_real_agent_execution=false`，`real_agents_started=false`。
- MCP mutation tools：MCP surface 仍只有 `status,list_tasks,get_task,recent_events`。
- GitHub write API / PR mutation / PR comment：`github_write_api_called=false`、`pr_comment_performed=false`、`pr_mutation_performed=false`。
- 业务仓写入：`target_writes_performed=false`；不得写入 `D:/example/protected-business-repo`。
- push / merge / deploy：`git_push_performed=false`、`git_merge_performed=false`、`git_deploy_performed=false`。
- CodeX Automation 修改：`codex_automation_modified=false`。
- budget 消费、真实锁获取、stale recovery 执行：均只记录预检证据。

## 3. 实现摘要

### 3.1 Schema v9

`aiwg/state/database.py`：

- `SCHEMA_VERSION = 9`
- 新 migration：`phase_d5_1_preflight_controls`
- 新表：
  - `d5_budget_preflight`
  - `d5_checkpoint_lease_preflight`
  - `d5_external_review_fixture_ingest`

关键 fail-closed CHECK：

- `d5_budget_preflight.consumed_budget_usd = 0`
- `d5_budget_preflight.dry_run = 1`
- `d5_checkpoint_lease_preflight.real_lock_acquired = 0`
- `d5_checkpoint_lease_preflight.stale_recovery_performed = 0`
- `d5_checkpoint_lease_preflight.reset_to_ready_performed = 0`
- `d5_external_review_fixture_ingest.read_only = 1`
- `github_write_api_called/pr_comment_performed/pr_mutation_performed/created_fix_tasks/target_writes_performed/codex_automation_modified = 0`

### 3.2 Evaluator

`aiwg/d5_preflight.py`：

- `evaluate_d5_preflight(..., include_d5_1=False, external_review_fixture=None)`
- 默认仍兼容 D5.0。
- 显式 `include_d5_1=True` 后输出：
  - `phase=D5.1`
  - `d5_scope=D5.1-preflight`
  - `d5_1_components=[budget_preflight, checkpoint_lease_heartbeat_stale_recovery_precheck, external_review_fixture_ingest]`
  - `budget_preflight`
  - `checkpoint_lease_preflight`
  - `external_review_fixture_ingest`

### 3.3 CLI

`aiwg/cli.py d5-preflight` 新增：

- `--include-d5-1`
- `--external-review-fixture <path>`
- `--fail-on-blocked`

退出码语义：

- `0`：`passed_dry_run`；兼容模式下 `blocked` 仍可返回 `0`。
- `3`：使用 `--fail-on-blocked` 且 `status=blocked`。
- `2`：参数错误或非 dry-run。
- `1`：其他失败状态。

### 3.4 Dashboard

`aiwg/dashboard/status.py`：

- 最新 D5 preflight snapshot 可读出 D5.1 子组件。
- 文本展示包含：
  - `scope=D5.1-preflight`
  - `budget=within_budget|budget_exceeded`
  - `checkpoint_lease=checked`
  - `external_review_fixture=approved|blocked|...`

### 3.5 Fixture

新增本地只读 fixture：

- `docs/ai-workgroup/state/fixtures/d5-1-external-review-fixture.json`

该 fixture 仅用于本地 ingest preflight；不访问 GitHub、不评论 PR、不创建修复任务。

## 4. TDD 证据

### RED

命令：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/aiwg/preflight/test_d51_preflight.py -q -p no:cacheprovider
```

结果：`6 failed in 0.65s`

RED 覆盖点：

- schema 仍为 v8，缺少 D5.1 控制表。
- `evaluate_d5_preflight()` 不支持 `include_d5_1`。
- CLI 不支持 `--fail-on-blocked`。
- dashboard 不能展示 D5.1 子组件。

### GREEN / targeted

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/aiwg/preflight/test_d51_preflight.py -q -p no:cacheprovider
```

结果：`6 passed in 0.95s`

### D5.0 + D5.1 preflight regression

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/aiwg/preflight/test_d50_preflight.py \
  tests/aiwg/preflight/test_d51_preflight.py \
  -q -p no:cacheprovider
```

结果：`11 passed in 1.74s`

### Related regression

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/aiwg/preflight/test_d50_preflight.py \
  tests/aiwg/preflight/test_d51_preflight.py \
  tests/aiwg/state/test_a2_sqlite_import.py \
  tests/aiwg/state/test_b0_schema_hardening.py \
  tests/aiwg/review/test_d43_external_review_gate.py \
  tests/aiwg/dashboard/test_d42_role_health_contract.py \
  tests/aiwg/git/test_d4_git_steward_dry_run.py \
  tests/aiwg/runners/test_b7_operator_preflight_approval.py \
  -q -p no:cacheprovider
```

结果：`66 passed in 8.03s`

### Full suite

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

结果：`284 passed in 52.00s`

## 5. Smoke 验证

### D5.1 CLI

```bash
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli d5-preflight \
  --config aiwg.yaml \
  --workflow-id apf-preview-funnel \
  --target-root D:/example/protected-business-repo \
  --dry-run \
  --include-d5-1 \
  --external-review-fixture docs/ai-workgroup/state/fixtures/d5-1-external-review-fixture.json \
  --fail-on-blocked \
  --json
```

结果：

- `d51_cli_ok=true`
- `status=passed_dry_run`
- `phase=D5.1`
- `scope=D5.1-preflight`
- `budget=within_budget`
- `checkpoint_lease=checked`
- `external_review_fixture=approved`
- `target_writes_performed=false`
- `ready_for_real_agent_execution=false`
- `preflight_run_id=d51-apf-preview-funnel-de05b5a34cfd`

### Status dashboard

```bash
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli status --config aiwg.yaml --json
```

结果：

- `d51_status_ok=true`
- `phase=D5.1`
- `scope=D5.1-preflight`
- `status=passed_dry_run`
- `budget=within_budget`
- `checkpoint_lease=checked`
- `external_review_fixture=approved`
- `mcp_mutation_tools_exposed=false`
- `target_writes_performed=false`

### Doctor

```bash
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli doctor --config aiwg.yaml
```

结果：`AIWG doctor: OK`

备注：仍有非阻塞 warning：当前目录不是 git repository。该 warning 不改变 dry-run/preflight 安全结论。

### MCP surface

```bash
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.mcp.server --config aiwg.yaml --list-tools
```

结果：

```text
status
list_tasks
get_task
recent_events
```

仍只有 4 个 read-only tools。

### SQLite evidence

- `sqlite_user_version=9`
- `d5_preflight_runs=4`
- `d5_artifact_provenance=4`
- `d5_budget_preflight=5`
- `d5_checkpoint_lease_preflight=5`
- `d5_external_review_fixture_ingest=1`
- `bad_d5_safety_flags=0`
- `bad_d51_budget_consumption=0`
- `bad_d51_lease_mutation=0`
- `bad_d51_fixture_mutation=0`

## 6. 边界扫描

### Secret-like scan

扫描范围：D5.1 touched code/tests/fixture/tmp JSON。

结果：`secret_like_hits=0`

### 业务仓精确 marker scan

采用 CodeX 建议的精确 markers，不使用宽泛 `*d50*`：

- `d5_preflight`
- `phase-d5-preflight`
- `aiwg.d5_preflight`
- `d5_artifact_provenance`
- `d5_preflight_runs`
- `d5_budget_preflight`
- `d5_checkpoint_lease_preflight`
- `d5_external_review_fixture_ingest`
- `D5.1-preflight`

目标仓：`D:/example/protected-business-repo`

结果：

- `business_repo_marker_hits_total=0`
- 每个 marker 均为 `0`
- `business_repo_files_with_hits=0`

## 7. CodeX 复核关注点

建议 CodeX 重点复核：

1. `--fail-on-blocked` 是否满足 future CI/operator exit-code fail-closed 要求。
2. v9 tables 的 CHECK constraints 是否足够阻止预算消费、真实锁获取、stale recovery 执行、fixture ingest 变异行为。
3. Dirty fixture 是否只产生 `status=blocked` / `policy_denials`，不触发 PR comment / GitHub write / fix task。
4. Dashboard 是否只读读取 D5.1 rows，未创建新 MCP mutation surface。
5. 业务仓扫描是否使用精确 markers，避免 D5.0 复核指出的过宽口径。

## 8. Post-docs verification

文档与 acceptance 写入后再次验证：

- Targeted：`6 passed in 0.89s`
- Related regression：`66 passed in 7.77s`
- Full suite：`284 passed in 53.81s`
- Acceptance JSON：`d51_acceptance_json_ok=true`
- Doctor：`AIWG doctor: OK`
- MCP tools：`status,list_tasks,get_task,recent_events`
- Secret-like scan：`post_docs_secret_like_hits=0`
- Business repo precise marker scan：`post_docs_business_repo_marker_hits_total=0`，`post_docs_business_repo_files_with_hits=0`

## 9. 结论

D5.1 已完成 fake/dry-run/preflight-only 最小实现，当前状态可进入 independent review。不得据此进入 real agent execution、MCP mutation tools、GitHub write API、PR mutation/comment、业务仓写入或部署。
