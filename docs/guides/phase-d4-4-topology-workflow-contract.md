# Phase D4.4 — Versioned topology / workflow contract

## 状态

`D4.4` 已实现版本化 topology / workflow contract 的 dry-run / read-only 控制面切片，当前状态为 `codex_review_passed`。Post-fix independent review 与 CodeX 复核均已通过；本阶段只定义、解析、校验并展示机器可读契约，不调度真实 agent，不写入 AIVideoTrans 业务仓，不调用 GitHub write API，不评论 PR，不开放 MCP mutation tools。

## 背景

CodeX D4.3.1 复核通过后，建议优先吸收 Tutti 的版本化 topology + workflow 思路，先把“谁做什么、什么时候能推进、谁审核谁、什么 gate 才能进入下一步”写成机器可读配置，而不是依赖 Tech Lead 会话记忆。

D4.4 因此继续保持控制面优先：只做 schema、parser、validator、CLI/dashboard 展示和测试，为后续 D5-preflight / real execution 奠定更稳的契约基础。

## 新增产物

- `docs/ai-workgroup/topology/aiwg.topology.v1.yaml`
  - `schema_version=aiwg.topology.v1`
  - roles: `tech_lead`, `implementer`, `reviewer`, `git_steward`, `external_gate`
  - queues: 每个角色对应 inbox / outbox / ledger scope
  - capabilities: 明确 `can_self_review=false`、`can_push=false`、`can_merge=false`、`can_deploy=false`、`can_start_real_agents=false`、`can_write_protected_repo=false`、`can_expose_mcp_mutation_tools=false`
  - safety: `allow_real_agents=false`、`allow_write=false`、`allow_push=false`、`allow_merge=false`、`allow_deploy=false`、`allow_modify_codex_automations=false`、`mcp_mutation_tools_exposed=false`

- `docs/ai-workgroup/workflows/apf-preview-funnel.workflow.v1.yaml`
  - `schema_version=aiwg.workflow.v1`
  - checkpoints: `intake -> implement -> review -> external_review -> git_record`
  - gates: scope envelope, safe switches, write gate, worktree policy, no-self-review, reviewer verdict, external review gate, CI gate, Git Steward dry-run, protected write human gate
  - capability matrix: 每个角色显式声明权限边界
  - budget / timeout / retry policy
  - worktree policy: 写任务必须 worktree，主工作区写入不允许
  - human gate policy: protected business repo writes、real agent execution、GitHub write API、PR mutation、CodeX Automation 修改均需 human gate
  - post-review hardening: `pr_mutation_performed=false` 必须显式声明；validator 会拒绝 `can_modify_codex_automations=true`，并强制 `github_write_api_requires_human=true`、`pr_mutation_requires_human=true`、`codex_automation_modification_requires_human=true`

- `aiwg/topology.py`
  - topology YAML loader
  - topology validator
  - required role / queue / safety / forbidden mutation capability checks

- `aiwg/workflow_contract.py`
  - workflow YAML loader
  - workflow validator
  - read-only snapshot builder
  - text renderer
  - fail-closed validation snapshot

- `tests/aiwg/topology/test_d44_topology_workflow_contract.py`
  - 5 个 D4.4 targeted tests

## CLI / Dashboard

新增 CLI：

```bash
python -m aiwg.cli workflow-contract --config aiwg.yaml --json
```

关键输出：

```json
{
  "schema_version": "aiwg.workflow_contract_snapshot.v1",
  "read_only": true,
  "mutation_actions": [],
  "summary": {
    "role_count": 5,
    "checkpoint_count": 5,
    "gate_count": 10,
    "validation_passed": true
  },
  "validation": {
    "passed": true,
    "errors": [],
    "warnings": []
  },
  "ready_for_real_agent_execution": false,
  "ready_for_protected_business_repository_write": false,
  "mcp_mutation_tools_exposed": false
}
```

Dashboard `status` snapshot 现在包含 `workflow_contract` 段，并在文本渲染中展示：

```text
Workflow contract
- validation_passed=true | roles=5 | checkpoints=5 | read_only=true | mutation_actions=[]
```

## 安全边界

D4.4 不改变执行权限：

- `allow_real_agents=false`
- `allow_write=false`
- `allow_push=false`
- `allow_merge=false`
- `allow_deploy=false`
- `allow_secret_access=false`
- `allow_modify_codex_automations=false`
- `allow_destructive_commands=false`
- `ready_for_real_agent_execution=false`
- `ready_for_protected_business_repository_write=false`
- `mcp_mutation_tools_exposed=false`
- `target_writes_performed=false`
- `github_write_api_called=false`
- `git_push_performed=false`
- `git_merge_performed=false`
- `pr_comment_performed=false`
- `pr_mutation_performed=false`
- `created_fix_tasks=false`
- `codex_automation_modified=false`

MCP surface 仍只允许 4 个 read-only tools：

```text
status
list_tasks
get_task
recent_events
```

## TDD 记录

RED：

```text
python -m pytest tests/aiwg/topology/test_d44_topology_workflow_contract.py -q
-> exit 2 before aiwg.topology / aiwg.workflow_contract existed
```

Post-fix GREEN targeted：

```text
python -m pytest tests/aiwg/topology/test_d44_topology_workflow_contract.py -q
-> 5 passed in 0.37s
```

Independent review P1 RED / GREEN：

```text
python -m pytest tests/aiwg/topology/test_d44_topology_workflow_contract.py::test_workflow_validator_rejects_pr_mutation_codex_automation_and_missing_human_gates -q
RED -> failed: validate_workflow_contract returned passed=True for dirty PR mutation / CodeX Automation / human-gate fields
GREEN -> 1 passed in 0.10s
```

相关回归：

```text
python -m pytest tests/aiwg/topology/test_d44_topology_workflow_contract.py tests/aiwg/review/test_d43_external_review_gate.py tests/aiwg/dashboard/test_d42_role_health_contract.py tests/aiwg/state/test_a2_sqlite_import.py tests/aiwg/state/test_b0_schema_hardening.py -q
-> 37 passed in 4.40s
```

全量：

```text
python -m pytest -q
-> 273 passed in 52.40s
```

Doctor：

```text
python -m aiwg.cli doctor --config aiwg.yaml --project-root .
-> AIWG doctor: OK
```

非阻塞 warning 仍为：

```text
[WARN] not a git repository: Phase A0 will report this and will not run git init.
```

MCP：

```text
python -m aiwg.mcp.server --list-tools
-> status
-> list_tasks
-> get_task
-> recent_events
```

CLI smoke：

```text
Covered by post-docs targeted CLI subprocess test:
tests/aiwg/topology/test_d44_topology_workflow_contract.py -> 5 passed

Prior raw workflow-contract JSON smoke returned:
validation.passed=true, role_count=5, checkpoint_count=5, gate_count=10,
read_only=true, mutation_actions=[]

A later summarized pipe command was blocked by terminal consent guard and was not
used as evidence.
```

业务仓边界扫描：

```text
D:/example/protected-business-repo
*aiwg.topology.v1.yaml -> 0
*workflow.v1.yaml -> 0
*workflow_contract* -> 0
aiwg.topology.v1 / aiwg.workflow.v1 / workflow_contract_snapshot / D4.4 markers -> 0
```

Secret scan post-fix：

```text
guide / acceptance / topology YAML / workflow YAML / aiwg/topology.py /
aiwg/workflow_contract.py / D4.4 tests -> 0 matches
```

Acceptance JSON：

```text
python -m json.tool docs/ai-workgroup/state/artifacts/phase-d4-4-topology-workflow-contract/acceptance.json
-> acceptance_json_ok=true
```

## Independent review

第一次 post-docs independent review 发现一个 P1：workflow validator 未完全 fail-closed 覆盖 `pr_mutation_performed=true`、`capability_matrix.*.can_modify_codex_automations=true`、以及 `github_write_api_requires_human=false` / `pr_mutation_requires_human=false` / `codex_automation_modification_requires_human=false`。

修复后已新增 RED/GREEN 测试并更新 validator / workflow YAML / snapshot。Post-fix independent review 结论：

```json
{
  "passed": true,
  "blocking_issues": [],
  "security_concerns": [],
  "logic_errors": [],
  "safety_issues": []
}
```

reviewer 提出的两个非阻塞 clarity/doc 项也已处理：

- guide targeted tests 数量已从 4 更新为 5；
- workflow `capability_matrix` 已为所有角色显式补 `can_modify_codex_automations=false`。

处理后重新运行：

```text
python -m pytest tests/aiwg/topology/test_d44_topology_workflow_contract.py -q
-> 5 passed in 0.37s
```

## 非目标

D4.4 明确不做：

- 不启用 real agents
- 不创建真实 worktree lease
- 不执行真实写任务
- 不调用 GitHub write API
- 不评论 PR
- 不创建 fix tasks
- 不 push / merge / deploy
- 不开放 MCP mutation tools
- 不修改 CodeX Automations
- 不写入 AIVideoTrans 业务仓

## 下一步建议

D4.4 已通过 independent review 与 CodeX 复核。下一步进入 D5-preflight 规划；D5-preflight 仍必须保持 no real execution，重点验证：adapter runtime readiness、budget ledger、checkpoint lease/heartbeat/stale recovery、artifact provenance，以及 external gate read adapter 的只读 ingest。
