# Phase D5.2 — Preflight hardening (fake/dry-run only)

状态：`completed_ready_for_codex_review`
更新时间：`2026-06-07T11:48:02Z`

## 1. 目标

D5.2 在 D5.1 preflight controls 的基础上只做三项 hardening，继续严格停留在 fake/dry-run/preflight-only：

1. 显式记录 `fixture_declared_read_only`，区分“orchestrator ingest 行为是只读”和“fixture 自身声明为只读”。
2. 强化 `mutation_action_count > 0 => blocked` 语义约束：任何 fixture 声明或 source 中出现 mutation action，都只能进入 blocked 状态，不能被记录为 `ingested_read_only`。
3. 引入统一的 orchestrator evidence path guard：D5 evidence/artifact 路径只能落在 orchestrator 自有 `docs/ai-workgroup/state/artifacts` 下，且不得与 target/business repository root 重叠。

## 2. 非目标 / 禁止项

D5.2 不启用任何真实执行面：

- 不启动 real agents：`ready_for_real_agent_execution=false`，`real_agents_started=false`。
- 不暴露 MCP mutation tools：MCP surface 仍只有 `status,list_tasks,get_task,recent_events`。
- 不调用 GitHub write API，不评论/修改 PR：`github_write_api_called=false`、`pr_comment_performed=false`、`pr_mutation_performed=false`。
- 不写入 AIVideoTrans/business repository：`target_writes_performed=false`，boundary scan 要求 target repo 中无 D5.2 markers。
- 不 push/merge/deploy：`git_push_performed=false`、`git_merge_performed=false`、`git_deploy_performed=false`。
- 不修改 CodeX Automations：`codex_automation_modified=false`。
- 不获取真实锁、不消费预算、不执行 stale recovery；D5.1 语义保持 preflight 证据记录。

## 3. 实现摘要

### 3.1 Schema v10

`aiwg/state/database.py`：

- `SCHEMA_VERSION = 10`
- 新 migration：`phase_d5_2_preflight_hardening`
- harden 目标表：`d5_external_review_fixture_ingest`
- 新字段：`fixture_declared_read_only INTEGER NOT NULL DEFAULT 1 CHECK(fixture_declared_read_only IN (0, 1))`

新增 fail-closed semantic CHECK：

- `CHECK(fixture_declared_read_only = 1 OR status = 'blocked')`
- `CHECK(mutation_action_count = 0 OR status = 'blocked')`

迁移兼容性：

- 新库直接创建 v10 schema。
- 既有 v9 DB 会 rebuild `d5_external_review_fixture_ingest`：
  - 保留既有 rows；
  - 对 `mutation_action_count > 0` 或 `fixture_declared_read_only=0` 的 rows 自动归一为 `status='blocked'`、`gate_state='blocked'`；
  - `read_only` 字段继续表示 ingest 行为本身是只读，因此仍强制为 `1`。

### 3.2 Fixture semantics

`aiwg/d5_preflight.py`：

- `read_only`：orchestrator ingest 是否只读；D5.2 中始终保持 `true`，并通过 DB CHECK 防止 mutation flags。
- `fixture_declared_read_only`：fixture payload 和 sources 是否均声明 `read_only=true`。
- `mutation_action_count`：payload/source 中 mutation actions 的数量。

阻断规则：

- `fixture_declared_read_only=false` => `status=blocked`，并写入 `policy_denials`：`external_review_fixture.declared_not_read_only`。
- `mutation_action_count > 0` => `status=blocked`，并写入 `policy_denials`：`external_review_fixture.mutation_actions_present`。
- 即使 blocked，也不执行 PR comment、PR mutation、GitHub write API、target writes、fix task creation 或 CodeX Automation modification。

### 3.3 Unified evidence path guard

新增 `aiwg/evidence_paths.py`：

- `path_is_relative_to(path, parent)`：resolve 后判断包含关系。
- `paths_overlap(left, right)`：判断任一方向的 path containment，防止 evidence root 与 target root 互相包含。
- `assert_orchestrator_evidence_path(...)`：统一 evidence path contract。
- `assert_orchestrator_artifact_root(...)`：D5 artifact root 必须位于 `<project_root>/docs/ai-workgroup/state/artifacts` 下，并不得 overlap target roots。

D5 artifact 写入路径解析改为先调用该 guard，fail-closed error reason：

- `artifact_root_outside_orchestrator_artifacts`
- `artifact_root_overlaps_target_root`
- `d5_artifact_path_outside_artifact_root`

## 4. TDD 证据

### RED

命令：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/aiwg/preflight/test_d52_preflight_hardening.py -q -p no:cacheprovider
```

初始结果：失败（D5.2 目标功能尚不存在）。RED 覆盖点：

- schema 仍为 v9，缺少 `fixture_declared_read_only`。
- DB 无法表达 `mutation_action_count > 0 => blocked` semantic CHECK。
- dirty fixture 未区分 ingest read-only 与 fixture-declared read-only。
- artifact root 仍可能落在非 canonical orchestrator artifacts 目录或与 target root overlap。

### GREEN / targeted

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/aiwg/preflight/test_d52_preflight_hardening.py -q -p no:cacheprovider
```

结果：`4 passed in 0.33s`

### D5.1 + D5.2 targeted regression

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/aiwg/preflight/test_d51_preflight.py \
  tests/aiwg/preflight/test_d52_preflight_hardening.py \
  -q -p no:cacheprovider
```

结果：`10 passed in 1.18s`

### D5.0 + D5.1 + D5.2 preflight regression

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/aiwg/preflight/test_d50_preflight.py \
  tests/aiwg/preflight/test_d51_preflight.py \
  tests/aiwg/preflight/test_d52_preflight_hardening.py \
  -q -p no:cacheprovider
```

结果：`15 passed in 1.84s`

### Related regression

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/aiwg/preflight/test_d50_preflight.py \
  tests/aiwg/preflight/test_d51_preflight.py \
  tests/aiwg/preflight/test_d52_preflight_hardening.py \
  tests/aiwg/state/test_a2_sqlite_import.py \
  tests/aiwg/state/test_b0_schema_hardening.py \
  tests/aiwg/review/test_d43_external_review_gate.py \
  tests/aiwg/dashboard/test_d42_role_health_contract.py \
  tests/aiwg/git/test_d4_git_steward_dry_run.py \
  tests/aiwg/runners/test_b7_operator_preflight_approval.py \
  -q -p no:cacheprovider
```

结果：`70 passed in 8.00s`

### Full suite

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

结果：`288 passed in 55.55s`

## 5. Smoke 验证

### D5.2-compatible CLI smoke

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

结果摘要：

- `status=passed_dry_run`
- `phase=D5.1`
- `scope=D5.1-preflight`
- `preflight_run_id=d51-apf-preview-funnel-373dc0c1074c`
- `fixture_declared_read_only=true`
- `mutation_action_count=0`
- `target_writes_performed=false`
- `ready_for_real_agent_execution=false`
- artifact path 位于 `D:/AIGroup/ai-workgroup-orchestrator/docs/ai-workgroup/state/artifacts/phase-d5-preflight/`

> 备注：CLI surface 仍沿用 D5.1 flag/scope，因为 D5.2 是 hardening layer，不新增真实执行阶段或 mutation surface。

### SQLite evidence

- `PRAGMA user_version = 10`
- migration `(10, 'phase_d5_2_preflight_hardening')` 已安装。
- `d5_external_review_fixture_ingest` 包含 `fixture_declared_read_only`。
- `bad_fixture_semantics = 0`：不存在 `(mutation_action_count > 0 OR fixture_declared_read_only = 0) AND status <> 'blocked'` 的 rows。

### Doctor

```bash
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli doctor --config aiwg.yaml
```

结果：`AIWG doctor: OK`

备注：仍有既有非阻塞 warning：当前 orchestrator 目录不是 git repository。该 warning 不改变 fake/dry-run/preflight 安全结论。

### MCP surface

```bash
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.mcp.server --config aiwg.yaml --list-tools
```

结果仍只有 4 个 read-only tools：

```text
status
list_tasks
get_task
recent_events
```

### Business repo boundary scan

扫描 `D:/example/protected-business-repo` 的 D5/D5.2 精确 markers，结果为 0 hit；D5.2 未向业务仓写入 artifacts 或 markers。

## 6. Independent read-only review

结果：`passed`，无 blocking issues。

复核重点结论：

- `fixture_declared_read_only=false` 必然 blocked。
- `mutation_action_count>0` 必然 blocked。
- evidence/artifact path 只能落在 Orchestrator `docs/ai-workgroup/state/artifacts` 下。
- artifact root 与 `D:/example/protected-business-repo` overlap 会 fail-closed。
- MCP surface 仍只有 `status,list_tasks,get_task,recent_events` 四个只读工具。
- 未写 AIVideoTrans，未启用 real agents，未修改 CodeX Automation。

备注：`tests/aiwg/review/test_d43_external_review_gate.py` 仅作为 related regression command 覆盖，不列为 D5.2 changed-file evidence。

## 7. 变更文件

- `aiwg/evidence_paths.py`
- `aiwg/d5_preflight.py`
- `aiwg/state/database.py`
- `aiwg/dashboard/status.py`
- `tests/aiwg/preflight/test_d52_preflight_hardening.py`
- `tests/aiwg/preflight/test_d51_preflight.py`
- migration expectation updates：
  - `tests/aiwg/state/test_a2_sqlite_import.py`
  - `tests/aiwg/state/test_b0_schema_hardening.py`
  - `tests/aiwg/dashboard/test_d42_role_health_contract.py`
  - `tests/aiwg/git/test_d4_git_steward_dry_run.py`
  - `tests/aiwg/runners/test_b7_operator_preflight_approval.py`
- `docs/guides/phase-d5-2-preflight-hardening.md`
- `docs/ai-workgroup/state/artifacts/phase-d5-2-preflight-hardening/acceptance.json`

## 8. 下一步建议

D5.2 independent read-only review 已通过；当前可提交 CodeX quick review。D5.2 之后仍不进入 real execution，除非用户显式授权。
