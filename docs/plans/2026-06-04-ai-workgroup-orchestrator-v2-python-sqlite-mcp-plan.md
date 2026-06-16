# AI Workgroup Orchestrator v2：Python + SQLite + MCP 实施方案

> **日期：** 2026-06-04
> **适用项目：** `D:\AIGroup\ai-workgroup-orchestrator`
> **关联旧方案：** `docs/plans/2026-05-25-ai-agent-collaboration-orchestration-plan.md`
> **状态：** v2 Phase A 实施基线
> **核心结论：** 保留 Markdown 文件协议作为审计层，把实际任务状态、锁、事件、预算和恢复逻辑迁移到 Python + SQLite；通过 MCP 和 runner adapter 统一接入 Claude Code、Codex、OpenCode、Hermes control-plane / 可选 subagent bridge 以及未来其他 AI 编程工具。

---

## 1. 为什么需要 v2

旧方案已经验证了“文件队列 + watcher + runner”的基本可行性，并且已经落地了一批 Phase 0/1 组件：

- Markdown front matter 协议。
- `validate-message.ps1` 消息校验。
- `scan-inbox.ps1` / `watch-inbox.ps1`。
- `fake-runner.ps1`。
- `opencode-headless-runner.ps1`。
- `claude-headless-runner.ps1`。
- `check-runner-policy.ps1`。
- `Check-DiffScope.ps1`。
- `Invoke-ProjectAutopilotOnce.ps1`。
- Human dashboard。
- watcher、runner policy、human gate、stale claim、diff scope 等 smoke test。

但继续沿着 PowerShell + 文件锁扩展，会遇到几个结构性问题：

1. **状态分散**：状态散落在 Markdown、JSONL、lock 文件和脚本运行结果中，恢复和查询成本越来越高。
2. **并发复杂**：多个 runner 同时 claim、写报告、追加事件时，文件锁和 JSONL 只能解决最小问题，难以支撑复杂工作流。
3. **脚本网膨胀**：每加一个 gate、adapter、恢复逻辑，都要在多个 PowerShell 脚本里重复处理。
4. **Desktop runner 不可靠**：Codex Desktop / Claude Desktop / OpenCode Desktop 依赖登录态、权限弹窗、应用生命周期，不适合作为长期后台执行层。
5. **报告真实性不足**：agent 报告“测试已通过”不能直接作为事实，Orchestrator 应自己运行验收命令并记录结果。
6. **扩展 agent 困难**：接入新工具时，如果没有统一 adapter contract，后续会不断复制 runner 逻辑。

v2 的目标不是推翻旧方案，而是把旧方案中已经验证有效的协议、模板、policy、diff scope、Human Gate 迁移到更稳的控制平面。

---

## 2. v2 目标

### 2.1 核心目标

构建一个本地、多 agent、可审计、可恢复的自动开发控制平面：

```text
Human Requirement
  -> Python Orchestrator
  -> SQLite task state
  -> Markdown audit files
  -> Runner Adapter Layer
      -> Hermes control-plane / optional subagent bridge
      -> Claude Code headless / CLI
      -> Codex CLI / API adapter
      -> OpenCode CLI
      -> fake runner
      -> future workers
  -> Policy Gates
  -> Verification Gates
  -> Review Gates
  -> Human Dashboard
```

### 2.2 非目标

v2 第一阶段不追求：

- 完全无人生产部署。
- 自动 merge 到主分支。
- 自动 push 到远端。
- 自动修改 CodeX Desktop Automations。
- 自动执行 force push、rewrite history、清库、删库、部署、回滚等不可逆动作。
- 自动处理支付、价格、生产数据、密钥或高风险决策。
- 让多个 AI 在自由群聊里协商任务。
- 把 Codex Desktop / Claude Desktop 当成可靠后台服务。

---

## 3. 总体架构

### 3.1 分层设计

```text
ai-workgroup-orchestrator/
  docs/
    ai-workgroup/                  # 人类可读协议、审计文件、报告、模板
    plans/                         # 设计与实施计划

  aiwg/                            # 新增 Python package
    __init__.py
    cli.py                         # 命令行入口
    config.py                      # 配置加载
    paths.py                       # 路径规范化

    protocol/
      frontmatter.py               # Markdown + YAML front matter 解析
      schema.py                    # 消息 schema 和状态枚举
      markdown.py                  # 审计文件生成
      templates.py                 # 消息/报告模板

    state/
      db.py                        # SQLite 连接、迁移、事务
      migrations/                  # schema migration SQL
      repository.py                # task/event/lock 数据访问层

    orchestrator/
      intake.py                    # Human/CodeX 入口任务导入
      scheduler.py                 # 选择下一步任务
      dispatcher.py                # 调用 adapter
      reviewer.py                  # 复核/打回/关闭任务
      recovery.py                  # stale claim、失败恢复

    gates/
      policy.py                    # runner-policy
      scope.py                     # allowed_files / forbidden_files / diff scope
      human.py                     # Human Gate
      budget.py                    # 单任务/每日预算
      verification.py              # acceptance 命令执行与记录
      prompt_injection.py          # 外部资料隔离规则
      safety.py                    # global_pause / safe_mode / PAUSE_AUTOMATION

    adapters/
      base.py                      # RunnerAdapter contract
      fake.py
      hermes_bridge.py             # optional Hermes CLI/API/MCP bridge，默认只读
      claude_code.py
      codex_cli.py
      opencode.py
      external_command.py

    mcp/
      server.py                    # ai-workgroup MCP server
      tools.py                     # list/claim/write/update/assign 等工具

    dashboard/
      server.py                    # 可先迁移现有 human-dashboard-server.py
      views.py

    tests/
      fixtures/
      test_protocol.py
      test_state.py
      test_claim.py
      test_policy.py
      test_scope.py
      test_fake_runner.py
```

### 3.2 审计层与执行层分离

v2 保留 Markdown，但职责变化：

| 层 | 介质 | 职责 |
|---|---|---|
| 审计层 | `docs/ai-workgroup/**/*.md` | 人类可读、可 diff、可归档、可作为外部 agent prompt 输入 |
| 执行层 | `docs/ai-workgroup/state/tasks.sqlite` | 原子 claim、状态机、锁、事件、预算、恢复 |
| 事件层 | SQLite `events` 表；可导出 JSONL | dashboard、指标、失败追踪 |
| Adapter 层 | Python classes | 抹平不同 AI 工具调用差异 |
| MCP 层 | stdio/local MCP server | 让外部 agent 通过统一工具访问任务系统 |

原则：

- Markdown 是审计事实，不再是唯一执行事实。
- SQLite 是任务调度真相源。
- 每次状态变化都写 SQLite event。
- 关键状态变化可同步生成或更新 Markdown 文件。
- 文件变化可以触发导入，但 claim/update 必须走 SQLite 事务。

---

## 4. SQLite Schema 初稿

### 4.1 `tasks`

```sql
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  message_path TEXT NOT NULL,
  from_agent TEXT NOT NULL,
  to_agent TEXT NOT NULL,
  type TEXT NOT NULL,
  status TEXT NOT NULL,
  priority TEXT NOT NULL DEFAULT 'normal',
  requires_human INTEGER NOT NULL DEFAULT 0,
  can_write INTEGER NOT NULL DEFAULT 0,
  worktree_required INTEGER NOT NULL DEFAULT 0,
  max_scope TEXT NOT NULL DEFAULT 'limited',
  review_delegate TEXT,
  allowed_files_json TEXT NOT NULL DEFAULT '[]',
  forbidden_files_json TEXT NOT NULL DEFAULT '[]',
  context_files_json TEXT NOT NULL DEFAULT '[]',
  acceptance_json TEXT NOT NULL DEFAULT '[]',
  claimed_by TEXT,
  claimed_at TEXT,
  lock_id TEXT,
  attempt INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2,
  timeout_minutes INTEGER NOT NULL DEFAULT 30,
  max_budget_usd REAL,
  content_hash TEXT,
  frontmatter_hash TEXT,
  legacy_imported INTEGER NOT NULL DEFAULT 0,
  legacy_source_path TEXT,
  git_branch TEXT,
  worktree_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);
```

### 4.2 `events`

```sql
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT,
  message_id TEXT,
  agent TEXT NOT NULL,
  type TEXT NOT NULL,
  status TEXT,
  path TEXT,
  command TEXT,
  exit_code INTEGER,
  duration_ms INTEGER,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
```

### 4.3 `agent_runs`

```sql
CREATE TABLE agent_runs (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  adapter_type TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  timeout_seconds INTEGER,
  max_budget_usd REAL,
  prompt_path TEXT,
  stdout_path TEXT,
  stderr_path TEXT,
  report_path TEXT,
  exit_code INTEGER,
  error TEXT
);
```

### 4.4 `verification_runs`

```sql
CREATE TABLE verification_runs (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  command TEXT NOT NULL,
  cwd TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_ms INTEGER,
  exit_code INTEGER,
  stdout_path TEXT,
  stderr_path TEXT
);
```

### 4.5 `agent_capabilities`

```sql
CREATE TABLE agent_capabilities (
  agent TEXT PRIMARY KEY,
  adapter_type TEXT NOT NULL,
  can_read INTEGER NOT NULL DEFAULT 1,
  can_write INTEGER NOT NULL DEFAULT 0,
  can_run_shell INTEGER NOT NULL DEFAULT 0,
  can_review INTEGER NOT NULL DEFAULT 0,
  can_plan INTEGER NOT NULL DEFAULT 0,
  default_timeout_minutes INTEGER NOT NULL DEFAULT 30,
  daily_limit INTEGER,
  max_budget_usd REAL,
  config_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
```

### 4.6 `git_refs`

Git Steward 只记录版本流事实和 proposal，不替代业务审批。

```sql
CREATE TABLE git_refs (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  branch TEXT,
  worktree_path TEXT,
  base_branch TEXT,
  base_sha TEXT,
  head_sha TEXT,
  commit_proposed INTEGER NOT NULL DEFAULT 0,
  commit_sha TEXT,
  pr_proposed INTEGER NOT NULL DEFAULT 0,
  pr_url TEXT,
  ci_status TEXT,
  merge_ready INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

规则：

- `git_refs` 记录的是版本状态，不表示 Human 已批准 merge。
- `pr_url` 存在不代表可以 merge；merge 仍受 Human Gate 和 `allow_merge` 约束。
- CI 失败只写 `ci_status` 和 event，并生成修复任务，不直接修改原任务为 done。

---

## 5. 状态机

v2 保留旧方案状态，但由 SQLite 强制状态转换。

### 5.1 标准状态

```text
ready
claimed
working
reported
reviewing
needs_revision
needs_review
needs_clarification
waiting_human
waiting_codex
review_degraded
stale_claim
needs_manual_recovery
approved
done
cancelled
failed
archived
```

### 5.2 标准流

```text
ready
  -> claimed
  -> working
  -> reported
  -> reviewing
  -> approved
  -> done
```

### 5.3 修订流

```text
reviewing
  -> needs_revision
  -> ready
```

### 5.4 Human Gate

```text
ready / reviewing
  -> waiting_human
  -> approved / cancelled / ready
```

### 5.5 stale claim

```text
claimed / working
  -> stale_claim
  -> needs_manual_recovery / ready
```

写入任务进入 `stale_claim` 后不能自动回到 `ready`，必须先检查 diff 和工作树。

---

## 6. Orchestrator 核心流程

### 6.1 Intake

入口来源：

- Human 创建 Markdown instruction。
- CodeX / Hermes 生成任务。
- 旧版 `docs/ai-workgroup/inbox/*/*.md` 文件被扫描导入。
- MCP `write_message` 工具写入。

流程：

1. 解析 Markdown front matter。
2. 校验 schema。
3. 规范化路径。
4. 写入 `tasks`。
5. 写入 `events(type='task_imported')`。
6. 保留原 Markdown 文件作为审计文件。

### 6.2 Claim

必须使用 SQLite 事务：

```sql
UPDATE tasks
SET status='claimed', claimed_by=?, claimed_at=?, lock_id=?, attempt=attempt+1, updated_at=?
WHERE id=? AND status='ready' AND requires_human=0 AND attempt < max_attempts;
```

检查受影响行数：

- `1`：claim 成功。
- `0`：任务已被领取、状态变化、Human Gate、或超过尝试次数。

### 6.3 Dispatch

1. 根据 `to_agent` 查 adapter。
2. 运行 policy gate。
3. 若 `requires_human=true`，进入 `waiting_human`。
4. 若 `can_write=true`，确认 `allowed_files` 非空且不与 `forbidden_files` 冲突。
5. 创建 agent run。
6. 调用 adapter。
7. 捕获 stdout/stderr/exit code。
8. 写入 report。
9. 更新状态为 `reported` 或 `failed`。

### 6.4 Verification

Orchestrator 不相信 agent 自述的测试结果，必须自己运行 acceptance：

1. 读取 `acceptance_json`。
2. 每条命令在目标项目根目录执行。
3. 记录 stdout/stderr/exit code。
4. 写入 `verification_runs`。
5. 任一失败则进入 `needs_revision` 或 `failed`。

### 6.5 Review

Review 分两层：

1. **Spec review**：是否完成原任务要求。
2. **Quality review**：代码质量、安全、边界、测试覆盖。

可用 reviewer：

- Hermes 会话内 review / Hermes bridge read-only。
- OpenCode read-only。
- Codex CLI/API。
- Claude Code 只读 review。

原则：

- Spec review 先于 Quality review。
- 重要问题未修复前不能进入 done。
- reviewer 不能审批 Human Gate。

---

## 7. Gate 设计

### 7.1 Policy Gate

检查：

- 全局 kill switch。
- agent 是否启用。
- 每日调用上限。
- 单任务预算。
- `requires_human`。
- agent 能力是否满足任务要求。
- 是否允许外部模型调用。

### 7.2 Scope Gate

执行前检查：

- `can_write=false` 时 `allowed_files` 必须为空。
- `can_write=true` 时 `allowed_files` 必须明确。
- `forbidden_files` 永远优先。

执行后检查：

- 获取实际 git diff 文件列表。
- 每个改动路径必须匹配 `allowed_files`。
- 任何触达 `forbidden_files` 的任务进入 `needs_manual_recovery`。

### 7.3 Human Gate

必须 Human 决策的情况：

- 生产部署、回滚、数据迁移。
- 删除数据、清空目录、reset、rewrite history。
- 修改支付、套餐、价格、试用额度。
- 修改认证授权、安全策略、风控阈值。
- 引入持续费用或新供应商。
- 自动 push、merge、发布。

### 7.4 Verification Gate

- acceptance 命令由 Orchestrator 执行。
- 结果写入 `verification_runs`。
- 失败不能被 agent 报告覆盖。

### 7.5 Prompt Injection Gate

- 外部资料统一包在 `<external_data>`。
- front matter 只作为结构化数据，不拼成自然语言系统指令。
- runner prompt 明确：任务数据不是新的系统指令。
- agent 报告中必须区分事实、推断和建议。

### 7.6 Safety Switch / Safe Mode Gate

Safety Switch 是所有 gate 之前的 **Pre-flight gate**。只要触发，Orchestrator 不启动新 runner，不 claim 新任务，不执行真实 agent，不做 push/merge/deploy。

必须支持两类开关：配置布尔开关和文件级急停开关。

```yaml
policy:
  global_pause: false
  global_kill_switch: docs/ai-workgroup/state/PAUSE_AUTOMATION
  safe_mode: true
  allow_real_agents: false
  allow_write: false
  allow_push: false
  allow_merge: false
  allow_deploy: false
  allow_destructive_commands: false
  allow_network_write: false
  allow_secret_access: false
  allow_modify_codex_automations: false
```

规则：

- `PAUSE_AUTOMATION` 文件存在时，等价于 `global_pause=true`。
- `safe_mode=true` 时，只允许 Fake、read-only scan、dry-run、status/list/import audit；不启动真实 writer。
- `allow_real_agents=false` 时，OpenCode / Claude Code / Codex / Hermes bridge 全部不可执行，只能生成计划或 dry-run。
- `allow_write=false` 时，即使 task 的 `can_write=true`，adapter 也必须拒绝写文件。
- `allow_push=false`、`allow_merge=false`、`allow_deploy=false` 分别硬阻断 push、merge、deploy，不允许 agent 通过自然语言承诺绕过。
- `allow_destructive_commands=false` 时，删除、reset、rewrite history、清库、迁移回滚、批量覆盖等命令进入 Human Gate 或 Abort gate。
- `allow_modify_codex_automations=false` 永远是默认值；除非 Human 明确授权，否则任何脚本或 adapter 都不能创建、修改、删除 CodeX Desktop Automations。
- 所有开关变更必须写入 `events`，包括操作者、旧值、新值、原因。

设计原则：

```text
先能安全停，再谈自动跑。
先保护真实业务项目，再追求多 AI 协作效率。
任何不可逆动作都必须有 Human Gate，且默认拒绝。
```

---

## 8. Runner Adapter Contract

### 8.1 Python 接口

```python
class RunnerAdapter:
    agent_id: str
    adapter_type: str

    def prepare(self, task: Task, context: RunContext) -> PreparedRun:
        ...

    def run(self, prepared: PreparedRun) -> RunResult:
        ...

    def collect_report(self, result: RunResult) -> AgentReport:
        ...
```

### 8.2 Adapter 输出

```python
@dataclass
class RunResult:
    run_id: str
    agent: str
    status: Literal['ok', 'failed', 'timeout', 'cancelled']
    stdout_path: Path
    stderr_path: Path
    report_path: Path | None
    changed_files: list[str]
    exit_code: int | None
    duration_ms: int
    error: str | None
```

### 8.3 初始 adapters

| Adapter | 角色 | 默认权限 | 说明 |
|---|---|---|---|
| `fake` | 测试 | 读/写测试报告 | 不调用真实 AI |
| `hermes_bridge` | planner/reviewer/control-plane | 默认只读 / 受限 action | 通过 Hermes CLI/API/MCP bridge 接入；Phase D5/F 后启用，不假设 Python 可直接 import `delegate_task` |
| `claude_code` | implementer | 可写，严格 scoped | 主实现者 |
| `opencode` | reviewer/scout | 默认只读 | 低成本并行辅助 |
| `codex_cli` | planner/reviewer | 默认只读 | 不依赖 Codex Desktop |
| `external_command` | 通用 CLI | 按配置 | 未来接 Pi/OpenHands/Aider 等 |

---

## 9. MCP Server 设计

### 9.1 目标

MCP 负责给外部 AI 工具提供统一任务接口，避免每个 agent 自己解析 Markdown、抢锁、改状态。

### 9.2 工具列表

```text
list_inbox(agent, status='ready')
claim_message(agent, message_id)
get_message(message_id)
write_message(to, frontmatter, body)
update_status(message_id, status, reason)
record_decision(message_id, decision, reason, actor)
assign_task(from_agent, to_agent, task_spec)
list_tasks(status, agent=None)
get_task_context(task_id)
write_report(message_id, report)
record_event(message_id, type, payload)
request_human_decision(message_id, question, options)
```

### 9.3 Resources

```text
aiwg://protocol
aiwg://agent-registry
aiwg://inbox/{agent}
aiwg://task/{message_id}
aiwg://events/recent
```

### 9.4 MCP 安全边界

- MCP 工具不能绕过 Human Gate。
- `claim_message` 必须使用 SQLite 原子事务。
- `write_message` 必须通过 schema 校验。
- `update_status` 必须检查合法状态转换。
- 写入类工具必须记录 event。

---

## 10. 多 AI 协作模式

### 10.1 标准开发闭环

```text
Human requirement
  -> Hermes/CodeX planner creates task plan
  -> Claude Code implements scoped task
  -> Orchestrator checks diff scope
  -> Orchestrator runs acceptance
  -> OpenCode reviews read-only
  -> Hermes/CodeX does spec review
  -> needs_revision or done
```

### 10.2 两阶段 review

1. Spec reviewer：只看是否满足任务要求。
2. Quality reviewer：看代码质量、安全、测试覆盖、维护性。

### 10.3 多 agent 并行原则

- 多个 read-only reviewer 可以并行。
- 同一工作树同一时间只允许一个 writer。
- 写入任务优先使用独立 worktree。
- 同一文件的多个写入任务不能并发。
- OpenCode 默认不写核心业务代码。

### 10.4 v2 角色分工与工具 / 模型建议

v2 不完全沿用旧方案的“CodeX 控节奏、Claude Code 写代码、OpenCode 辅助”三角色结构。旧分工方向正确，但需要修改为：

```text
Python Orchestrator 是硬控制面
CodeX / Hermes 是 planner 和 reviewer
Claude Code 是主 implementer
OpenCode 是 scout / reviewer / 低风险 writer
Human 是产品、风险和最终审批人
```

也就是说，**调度、锁、状态、预算、diff scope、Human Gate 不再交给某个 LLM 角色决定，而由 Python Orchestrator 硬执行**。LLM 只做它擅长的规划、实现、审查和解释。

#### 10.4.1 角色矩阵

| 角色 | 主要职责 | 首选工具 | 推荐模型类型 | 默认写权限 | 说明 |
|---|---|---|---|---|---|
| Human | 提需求、产品取舍、风险审批、最终验收 | Dashboard / Hermes chat | 无 | 手动决策 | 只处理关键决策，不做日常转发 |
| Orchestrator | 状态机、claim、锁、预算、scope gate、verification、事件记录 | Python + SQLite | 无 LLM | 可写状态，不直接写业务代码 | 硬控制面，不能被 agent 绕过 |
| Intake / Clarifier | 把 Human 输入整理成可执行需求，发现缺口 | Hermes 或 CodeX | 高推理模型 | 只写任务/澄清文件 | 不直接改代码 |
| Planner / Decomposer | 拆任务、依赖排序、生成 acceptance | Hermes / CodeX / Codex CLI | 高推理 + 代码规划模型 | 只写任务文件 | 不作为单点中枢，输出必须过 schema |
| Implementer | scoped code change、docs/tests 修改、修复 reviewer 打回项 | Claude Code headless / CLI | Claude Sonnet 级主力 coding model；复杂重构可用更强模型 | 仅 `can_write=true` 且 `allowed_files` 范围内 | 主写手，必须由 Orchestrator 验证 diff 和 acceptance |
| Low-risk Writer | docs/tests/scripts 等窄范围修改 | OpenCode 或 Claude Code | 低/中成本 coding model，如 DeepSeek V4 Pro 或 Sonnet | 仅 docs/tests/scripts 等低风险范围 | 不写核心业务、认证、支付、迁移、部署 |
| Scout | 代码侦察、相关文件定位、风险扫描、测试缺口分析 | OpenCode | 低成本长上下文模型，如 DeepSeek V4 Pro | 否 | 并行、只读，降低主模型成本 |
| Spec Reviewer | 对照原任务检查是否做完、是否越界 | Hermes / CodeX / Codex CLI | 高推理模型 | 否 | Spec review 先于 quality review |
| Quality Reviewer | 代码质量、安全、可维护性、测试覆盖 | OpenCode + 必要时 Claude/Codex 二审 | 低成本 reviewer 先扫；高风险用强模型复核 | 否 | reviewer 不能审批 Human Gate |
| Test Designer | 设计缺失测试、补 acceptance、解释失败 | OpenCode 或 Claude Code | 中等 coding/test model | 默认否；补 tests 时可有限写 | acceptance 实际运行仍由 Orchestrator 做 |
| SecurityGuard | secrets、注入、认证授权、路径穿越、危险命令 | Hermes / Codex / Claude read-only | 高推理/安全审查模型 | 否 | 高风险发现直接进入 Human/needs_review |
| OpsGuard | 部署、迁移、环境、CI/CD 风险审查 | Hermes / Claude read-only | 高推理模型 | 否 | 不自动部署、不自动回滚 |
| Git Steward | branch、worktree、commit proposal、PR proposal、CI 状态回流 | Python Orchestrator + git/gh CLI；必要时 Hermes/CodeX read-only 辅助 | 无 LLM，或低/中推理辅助解释 CI | 默认不 push、不 merge；只写版本事件和 proposal | 只处理版本流，不做业务判断；CI 失败生成 `needs_revision` / diagnosis 任务 |
| DocWriter | runbook、FAQ、开发说明、日报摘要 | OpenCode / Claude Code | 中低成本写作/代码文档模型 | docs-only | 仍需 schema 和 report 校验 |

#### 10.4.2 工具与模型选择原则

1. **硬控制不用 LLM**：状态、锁、预算、scope、acceptance、Human Gate 由 Python 代码执行。
2. **主实现优先 Claude Code**：Claude Code print/headless 模式适合自动化实现；用 `--allowedTools`、`--max-turns`、`--max-budget-usd` 限制范围和成本。
3. **OpenCode 默认只读**：OpenCode + DeepSeek V4 Pro 适合 scout、review、测试建议、文档建议；只有 `can_write=true` 且 `allowed_files` 限定到 `docs/**`、`tests/**`、`scripts/ai-workgroup/**` 时才可写。
4. **CodeX / Codex 不再做硬中枢**：Codex 适合 planner、spec reviewer、patch reviewer；不依赖 Codex Desktop Automation。
5. **Hermes 能力要清晰使用，不神化也不低估**：Hermes 可以在当前会话中读写文件、运行命令、调用 `delegate_task` subagents、安排 cron、作为 MCP client 使用 `aiwg` MCP server，也可以通过 CLI/PTY 启动 Claude Code / Codex / OpenCode / Hermes 子进程；但独立 Python Orchestrator 不能直接 import Hermes 会话内的 `delegate_task`，也不能无验证地向 Codex Desktop GUI 注入消息。Hermes 适合作为人工监督、规划、review 和上层控制面，Phase A/B 不把它放进必需执行闭环。
6. **高风险任务用强模型复核**：认证授权、支付、迁移、部署、安全相关任务至少需要一个高推理 reviewer，并通常进入 Human Gate。
7. **便宜模型先扫，强模型裁决**：Scout / 初筛 review 可用低成本模型；最终 spec/安全/架构裁决用高推理模型。
8. **模型名不写死**：实际模型从 `aiwg.yaml` 或各工具本机配置读取。计划中只指定能力等级，例如 `high_reasoning`、`primary_coding`、`low_cost_review`。

#### 10.4.3 推荐初始 agent 配置

```yaml
agents:
  Fake:
    role: test_runner
    adapter: fake
    enabled: true
    model_tier: none
    can_write: false

  Git-Steward:
    role: git_steward
    adapter: git
    enabled: false
    model_tier: none
    can_write: false
    allow_auto_commit: false
    allow_auto_push: false
    allow_auto_pr: false
    allow_auto_merge: false

  OpenCode-Scout:
    role: scout
    adapter: opencode
    enabled: false
    model_tier: low_cost_review
    preferred_model: deepseek-v4-pro
    can_write: false

  OpenCode-Reviewer:
    role: quality_reviewer
    adapter: opencode
    enabled: false
    model_tier: low_cost_review
    preferred_model: deepseek-v4-pro
    can_write: false

  Claude-Implementer:
    role: implementer
    adapter: claude_code
    enabled: false
    model_tier: primary_coding
    preferred_model: claude-sonnet
    can_write: true
    allowed_write_classes: [docs, tests, scripts, limited_code]

  Codex-Planner:
    role: planner
    adapter: codex_cli
    enabled: false
    model_tier: high_reasoning
    can_write: false

  Codex-SpecReviewer:
    role: spec_reviewer
    adapter: codex_cli
    enabled: false
    model_tier: high_reasoning
    can_write: false
```

`preferred_model` 只是偏好，不是硬编码；adapter 启动时应允许本机配置覆盖。

#### 10.4.4 推荐首版启用顺序

```text
Phase A0-A3: Fake only，完成 Python + SQLite 最小闭环
Phase A4: read-only status/dashboard endpoint，只读 SQLite，不写业务状态
Phase B: Fake + deterministic gates + Git Steward dry-run
Phase C: MCP tools，仍以 Fake 验证；Hermes 可作为 MCP client 读取/审查状态
Phase D1: OpenCode-Scout / OpenCode-Reviewer read-only
Phase D2: Codex-Planner / Codex-SpecReviewer read-only
Phase D3: Claude-Implementer docs/tests/scripts scoped write
Phase D4: Claude-Implementer limited_code scoped write
Phase E: Dashboard + metrics + optional Hermes control-plane integration；Human buttons 只调用受限 action
```

这样可以避免一开始就让多个真实 AI writer 同时介入，先把控制面和低风险 read-only 协作跑稳。

#### 10.4.5 Hermes 的实际能力与稳健边界

CodeX 对 Hermes 的能力可能会偏保守，因此本方案需要把 Hermes 的真实能力和边界写清楚：

**Hermes 可以可靠承担的角色：**

- 在 Human 会话里作为 planner / reviewer / coordinator，直接读取方案、改文档、运行验证命令。
- 用 `delegate_task` 同步派发隔离 subagent，适合短时并行侦察、两阶段 review、计划审查。
- 用 `terminal(background=true, notify_on_complete=true)` 或 cronjob 管理较长的本机任务，但需要明确生命周期和输出路径。
- 作为 MCP client 连接 `aiwg` MCP server，读取任务、生成 review、提交受限 action。
- 通过 CLI/PTY 启动并监控 Claude Code、Codex CLI、OpenCode、Hermes 子进程。

**Hermes 不应被误用的地方：**

- 不把 Hermes 当前聊天上下文当成生产级任务状态真相源；状态真相源仍是 SQLite。
- 不要求独立 Python Orchestrator 直接调用会话内 `delegate_task`；如果需要，应通过 Hermes CLI/API/MCP bridge 明确定义 adapter。
- 不依赖 Codex Desktop / Claude Desktop GUI 自动化作为可靠 runner；Desktop 只适合人机界面或 spike。
- 不让 Hermes 或任何 agent 绕过 Scope Gate、Verification Gate、Human Gate、Safety Switch。

因此，本方案不是“因为 Hermes 弱所以保守”，而是：**Hermes 能做很多上层协调和审查，但控制面必须可重放、可测试、可停机、可审计；这些职责应落在 Python + SQLite + gate 代码里。**

---

## 11. Worktree 策略

### 11.1 分支命名

```text
aiwg/<task-id>-<message-id>
```

示例：

```text
aiwg/APF2-APF2-msg-001
```

### 11.2 何时必须 worktree

- `can_write=true`。
- `max_scope=module` 或更大。
- 任务可能与当前工作树冲突。
- 多个 writer 可能并行。

### 11.3 合并策略

- Orchestrator 可以生成 patch 或 branch。
- 不自动 merge 到主分支。
- Human 或明确授权的 CodeX/Hermes gate 审批后再合并。

### 11.4 Git / PR / CI Stewardship

Git Steward 是版本流守门员，不是业务决策者。它只回答：当前改动能不能安全形成 commit / PR proposal，以及 CI 结果如何回流到任务系统。

#### 11.4.1 前置条件

- 项目必须先是 git repository；当前项目尚未初始化 git 时，Phase A0 只能生成初始化建议，不自动 `git init`，除非 Human 明确确认。
- 每个 writer task 使用独立 branch 或 worktree。
- 开始写入前必须 `git status --short` 并记录 event；如果主工作树有未归属改动，进入 Human Gate。
- 禁止复用 Human 正在编辑的工作树作为 writer 工作区。

#### 11.4.2 commit 允许条件

只有满足以下条件，Git Steward 才能生成 commit proposal 或在授权后执行 commit：

1. task 至少到 `reported` 或 `verification_passed`。
2. Scope Gate pass：实际 diff 文件列表完全在 `allowed_files` 内。
3. Verification Gate pass：acceptance 命令实际运行成功。
4. 无 forbidden file、secret、`.env`、credential、生产配置被触达。
5. 无 pending Human Gate。
6. branch/worktree 与 `message_id` 绑定。
7. commit message 包含 `task_id` / `message_id`，且使用 Conventional Commit 风格。

默认配置：

```yaml
git:
  allow_auto_commit: false
  allow_auto_push: false
  allow_auto_pr: false
  allow_auto_merge: false
```

即使 `allow_auto_commit=true`，push / PR / merge 仍分别需要独立开关，不得连带授权。

#### 11.4.3 push / PR / merge 规则

- `allow_push=false` 时，只允许生成 `git push` 建议，不执行。
- `allow_auto_pr=false` 时，只生成 PR body draft 和 changed-files summary，不调用 `gh pr create`。
- `allow_merge=false` 时，永远不执行 `gh pr merge`、`git merge`、`git rebase --onto`、`git push --force`。
- 任何 force push、history rewrite、branch deletion 都进入 Human Gate；默认拒绝。
- merge readiness 由 Git Steward 汇总，但最终由 Human 或明确授权的 gate 决定。

#### 11.4.4 CI 回流

CI 是事实来源之一，但不是业务审批来源：

```text
PR / branch CI fails
  -> record event(ci_failed)
  -> store failed logs under artifacts/runs/<run-id>/ci/
  -> create needs_revision or diagnosis task
  -> assign to Implementer / Test Designer / OpsGuard
```

CI 失败时不得“顺手修更多东西”。修复任务仍必须有新的 `allowed_files`、acceptance 和 scope gate。

#### 11.4.5 禁止事项

- Git Steward 不判断需求是否正确，不批准业务行为。
- 不自动修改 CodeX Desktop Automations。
- 不自动部署、不自动回滚、不自动改生产环境变量。
- 不在主分支直接写业务代码。
- 不把未通过 verification 的 agent 报告包装成成功状态。

---

## 12. 从现有 PowerShell 迁移

### 12.1 保留

以下内容应保留并逐步迁移：

- `docs/ai-workgroup/00-protocol.md`。
- `docs/ai-workgroup/shared/*`。
- `validate-message.ps1` 的规则。
- `check-runner-policy.ps1` 的策略。
- `Check-DiffScope.ps1` 的路径校验。
- Human dashboard 的产品形态。
- smoke test fixtures。

### 12.2 迁移顺序

1. `aiwg.yaml` 配置加载和 safe defaults。
2. Python front matter parser。
3. Python schema validator。
4. SQLite schema + migrations。
5. Legacy migration audit report，只审计不执行旧任务。
6. fake adapter。
7. claim/update/event repository。
8. Python Safety Switch / Policy Gate。
9. Python diff scope gate。
10. Python acceptance runner。
11. Git Steward dry-run，只生成 proposal。
12. MCP server。
13. OpenCode read-only adapter。
14. Codex CLI/API read-only adapter。
15. Claude Code scoped writer adapter。
16. Dashboard 读取 SQLite。
17. PowerShell watcher 降级为启动器/兼容入口。

### 12.3 PowerShell 新定位

PowerShell 不再承载核心调度，只作为：

- Windows Task Scheduler 启动器。
- 向后兼容入口。
- 一键 smoke test wrapper。
- 本机环境探测脚本。

### 12.4 旧消息迁移策略

v2 启动时不能把 `docs/ai-workgroup` 里的历史消息直接当成新任务执行。旧消息可能已经完成、失败、被人工处理、或语义上被新方案 supersede；重复执行会污染业务项目。

迁移模式必须显式配置：

```yaml
legacy_migration:
  mode: audit_only
  write_report: true
  report_path: docs/ai-workgroup/state/legacy-migration-report.md
  import_terminal: false
  import_ready: false
  require_human_selection: true
```

可选模式：

| mode | 行为 | 默认是否允许执行 |
|---|---|---|
| `audit_only` | 只扫描、分类、生成报告，不导入为可执行 task | 否 |
| `manual_selection` | Human 在报告里勾选要导入的 message | 否，除非选中 |
| `import_ready_only` | 只导入符合 schema 且 non-terminal 的 ready message | 仍需 `allow_real_agents` |
| `import_all_nonterminal` | 导入所有 non-terminal message | 不建议，需 Human Gate |

迁移报告至少包含：

- 扫描到的 message 总数。
- terminal / non-terminal / invalid / duplicate 分类。
- 每个旧 message 的 `message_id`、path、status、to_agent、can_write、requires_human。
- schema validation 错误。
- 与 SQLite 已有记录的冲突。
- 建议动作：`audit_only`、`legacy_imported`、`superseded`、`archived`、`manual_review`。

导入规则：

1. terminal 状态默认只保留审计，不重新执行。
2. `ready` / `blocked` / `needs_revision` 等 non-terminal 状态默认进入 `manual_review`，除非 Human 选择导入。
3. 导入后的任务增加 `legacy_imported=true`、`legacy_source_path`、`legacy_imported_at`。
4. 如果旧 Markdown front matter 与 SQLite 冲突，SQLite 不被覆盖，任务进入 `needs_manual_recovery`。
5. legacy import 永远不触发真实 agent；只能由后续显式 run 命令执行。

---

## 13. 测试计划

### 13.1 单元测试

- front matter 解析。
- schema 校验。
- 状态转换。
- claim 原子性。
- policy gate。
- scope gate。
- budget gate。
- Human Gate。

### 13.2 集成测试

- fake runner happy path。
- malformed message。
- lock conflict。
- stale claim。
- requires_human 阻断。
- can_write=false 但 allowed_files 非空。
- forbidden_files 被触达。
- acceptance 命令失败。
- reviewer 打回。

### 13.3 真实 agent smoke test

按风险从低到高：

1. OpenCode read-only review。
2. Hermes MCP/read-only review。
3. Codex CLI read-only planning/review。
4. Claude Code docs/tests scoped write。
5. Claude Code limited implementation。

### 13.4 Git / Migration / Safety 回归测试

- `PAUSE_AUTOMATION` 文件存在时，`run-once` 不 claim、不 dispatch。
- `safe_mode=true` 时，Fake/list/import audit 可运行，真实 agent 被拒绝。
- `allow_write=false` 时，`can_write=true` task 也不会启动 writer。
- `allow_push=false` / `allow_merge=false` 时，只生成 proposal，不执行 git push/merge。
- `allow_modify_codex_automations=false` 时，任何试图触达 CodeX Automations 的 adapter/action 被拒绝并写 event。
- legacy migration `audit_only` 只生成报告，不导入可执行 task。
- legacy `manual_selection` 只导入被 Human 选择的 message。
- 重复 legacy import 不生成重复 task。
- 非 git repo 下 Git Steward 只报告前置条件缺失，不自动 `git init`。
- CI failure fixture 会生成 `needs_revision` / diagnosis task，而不是把原任务标记 done。

---

## 14. 分阶段实施计划

### Phase A：Python skeleton + SQLite + Fake 最小闭环

Phase A 不接真实 AI writer，不 push，不 merge，不部署。目标是证明“一个任务可靠流转”，而不是展示多 AI 自动协作。

#### A0：项目骨架与安全默认值

交付：

- `pyproject.toml`。
- `aiwg/` Python package。
- `python -m aiwg.cli --help`。
- `aiwg.yaml` 默认安全配置。
- `PAUSE_AUTOMATION` / `safe_mode` / `allow_real_agents=false` 检查。
- git repo 前置检查：只报告当前不是 git repo，不自动初始化。

验收命令：

```bash
python -m pytest -q
python -m aiwg.cli --help
python -m aiwg.cli doctor --config aiwg.yaml
```

#### A1：front matter parser + schema validator

交付：

- Markdown front matter parser。
- schema validator。
- 复用旧 fixtures。
- `can_write=false` 时 `allowed_files` 必须为空。

验收命令：

```bash
python -m pytest tests/aiwg/protocol -q
python -m aiwg.cli validate-message docs/ai-workgroup/.../message.md --config aiwg.yaml
```

#### A2：SQLite init / migrations / import / list

交付：

- SQLite schema + `schema_migrations`。
- WAL / busy_timeout / foreign_keys。
- `import-inbox`。
- `list-tasks`。
- `legacy-migration-report` 的 `audit_only` 模式。

验收命令：

```bash
python -m aiwg.cli init-db --config aiwg.yaml
python -m aiwg.cli import-inbox --config aiwg.yaml --agent Fake --dry-run
python -m aiwg.cli legacy-audit --config aiwg.yaml
python -m aiwg.cli list-tasks --config aiwg.yaml
```

#### A3：atomic claim + fake adapter + event log happy path

交付：

- atomic claim。
- stale claim 基础恢复。
- Fake adapter。
- event logging。
- artifact 路径落盘。

成功标准：

```text
导入 Markdown task -> SQLite ready -> fake claim -> fake report -> done
```

验收命令：

```bash
python -m aiwg.cli run-once --config aiwg.yaml --agent Fake
python -m aiwg.cli list-tasks --config aiwg.yaml --status done
python -m pytest tests/aiwg/state tests/aiwg/runners -q
```

#### A4：read-only status / dashboard endpoint

交付：

- 只读状态查询 API 或 CLI endpoint。
- dashboard 只能读取 SQLite 和 event summary。
- 不提供 done/approve/merge 等写操作按钮。

成功标准：

```text
Human 可以看到任务状态、最近 event、artifact 链接；不能绕过 Orchestrator gate 改业务状态。
```

### Phase B：Policy / Scope / Verification Gates

交付：

- Safety Switch / Safe Mode gate。
- Python policy gate。
- Python scope gate。
- acceptance 命令执行器。
- verification_runs 表。
- diff scope 检查。
- Human Gate 阻断。
- Git Steward dry-run：生成 commit / PR proposal，不 push、不 merge。

成功标准：

- `PAUSE_AUTOMATION` 存在时不会 claim/dispatch。
- `allow_real_agents=false` 时真实 agent 不会启动。
- `requires_human=true` 不会被执行。
- forbidden file diff 会进入 `needs_manual_recovery`。
- acceptance 失败会阻止 done。
- Git Steward 在非 git repo 或未授权 push/merge 时只报告，不执行。

### Phase C：MCP Server

交付：

- stdio MCP server。
- `list_inbox` / `claim_message` / `write_report` / `update_status` / `record_decision`。
- 外部 agent 通过 MCP 查询任务。
- Hermes MCP client 配置样例。

成功标准：

- OpenCode / Claude / Hermes 可以通过同一 MCP 工具读写受限任务状态，而不是直接解析文件。
- MCP 写操作仍走 Orchestrator gate；dashboard 和 Hermes 不能直接改 SQLite 绕过 gate。

### Phase D：真实 Agent Adapters

交付：

- D1：OpenCode read-only scout/reviewer adapter。
- D2：Codex CLI/API read-only planner/spec reviewer adapter。
- D3：Claude Code docs/tests/scripts scoped writer adapter。
- D4：Claude Code limited_code scoped writer adapter。
- D5：Hermes control-plane / optional subagent bridge，只有在 MCP/CLI 边界验证后启用。

成功标准：

```text
Human task -> planner -> Claude scoped implementation -> Orchestrator verification -> OpenCode/Codex/Hermes review -> done/needs_revision
```

Phase D 每一步都必须可以单独回滚到 Fake/read-only 模式。

### Phase E：Dashboard + Metrics

交付：

- Dashboard 读取 SQLite。
- Human decision cards。
- agent 成功率、耗时、打回率统计。
- 每日摘要。
- Human action 全部调用 Orchestrator API/MCP 受限 action，不直接写业务状态。

成功标准：

- Human 只看 dashboard 和决策卡，不做消息转发。

---

## 15. 目录是否单独创建

当前建议：**不用为计划单独新建目录**。

理由：

- 现有旧方案已经在 `docs/plans/`。
- 新方案是同一项目的后续方案，放在同一目录便于对照。
- `docs/plans/` 目前只有两个计划文件，不存在拥挤问题。

如果后续计划文件超过 10 个，再考虑拆分：

```text
docs/plans/
  ai-workgroup-orchestrator/
    2026-05-25-ai-agent-collaboration-orchestration-plan.md
    2026-06-04-ai-workgroup-orchestrator-v2-python-sqlite-mcp-plan.md
```

但现在推荐保持：

```text
docs/plans/2026-06-04-ai-workgroup-orchestrator-v2-python-sqlite-mcp-plan.md
```

---

## 16. 推荐下一步

1. 确认本方案方向。
2. 创建 `aiwg/` Python package skeleton。
3. 实现 SQLite schema migration。
4. 实现 Markdown import + fake runner happy path。
5. 把现有 `validate-message.ps1` 规则迁为 Python schema validator。
6. 用现有 fixtures 做回归测试。

第一段开发目标不要接真实 AI，先证明控制平面本身稳定：

```text
Python Orchestrator + SQLite + fake adapter + tests
```

只要这个闭环稳定，再接 OpenCode / Claude / Codex / Hermes 才不会把调度问题和模型问题混在一起。

---

## 17. 自审补充：v2 草案需要补强的地方

本节是对本方案的二次自审结果。整体方向成立，但如果直接进入实现，还需要补足以下设计细节，避免 Phase A/B 后返工。

### 17.1 配置文件必须先定义

当前方案有目录和 schema，但还缺少项目级配置文件。建议新增：

```text
aiwg.yaml
```

示例：

```yaml
project_root: .
workgroup_root: docs/ai-workgroup
state_db: docs/ai-workgroup/state/tasks.sqlite
artifact_root: docs/ai-workgroup/state/artifacts
logs_root: docs/ai-workgroup/state/logs

shell:
  windows: powershell
  timeout_seconds_default: 180

agents:
  Fake:
    adapter: fake
    enabled: true
    can_write: false
  OpenCode:
    adapter: opencode
    enabled: false
    can_write: false
  Claude-Code:
    adapter: claude_code
    enabled: false
    can_write: true
  Codex:
    adapter: codex_cli
    enabled: false
    can_write: false

policy:
  global_pause: false
  global_kill_switch: docs/ai-workgroup/state/PAUSE_AUTOMATION
  safe_mode: true
  allow_real_agents: false
  allow_external_agents: false
  allow_write: false
  allow_push: false
  allow_merge: false
  allow_deploy: false
  allow_destructive_commands: false
  allow_network_write: false
  allow_secret_access: false
  allow_modify_codex_automations: false
  default_timeout_minutes: 30
  default_max_attempts: 2

git:
  enabled: false
  default_base_branch: main
  allow_auto_commit: false
  allow_auto_push: false
  allow_auto_pr: false
  allow_auto_merge: false

legacy_migration:
  mode: audit_only
  import_terminal: false
  import_ready: false
  require_human_selection: true
```

理由：

- 不要把 agent 命令、模型名、本机路径、预算、是否启用外部 agent 写死在代码里。
- `aiwg.yaml` 可提交默认安全配置；个人密钥、token、私有路径应放 `.env` 或本机 override，不进 git。
- Phase A 应先实现配置加载，否则 adapter 和 CLI 入口会很快散落硬编码。
- 所有真实 agent、写文件、push、merge、deploy、网络写入、密钥读取、CodeX Automation 修改都默认关闭；需要 Human 显式打开。
- `PAUSE_AUTOMATION` 是文件级急停开关，适合在 dashboard、脚本和手工操作之间共享。
- git 和 legacy migration 配置必须独立于 agent 配置，防止“启用某个 agent”意外连带开启 push/merge/旧任务执行。

### 17.2 SQLite 需要补充约束、索引和并发设置

当前 schema 是初稿，还需要加：

- `PRAGMA journal_mode=WAL;`
- `PRAGMA busy_timeout=5000;`
- `PRAGMA foreign_keys=ON;`
- `schema_migrations` 表。
- `UNIQUE(message_path)`，避免重复导入。
- `CHECK(status IN (...))` 或代码层强校验状态枚举。
- `CHECK(requires_human IN (0,1))` 等布尔字段约束。
- 常用索引：
  - `(status, to_agent, priority, created_at)`。
  - `(task_id)`。
  - `(claimed_by, claimed_at)`。
  - `events(message_id, created_at)`。
  - `git_refs(message_id)`。
  - `git_refs(ci_status, updated_at)`。

还应明确：

- `tasks.id` 建议就是 message id，例如 `T42-msg-001`。
- `task_id` 是业务任务组 id，例如 `T42`。
- 所有时间统一存 ISO 8601，建议 UTC；展示给 Human 时再本地化。

### 17.3 Markdown 与 SQLite 同步策略需要写清楚

目前只说“Markdown 是审计层，SQLite 是执行层”，但还缺少冲突策略。

建议规则：

1. Markdown 初次出现时可被导入 SQLite。
2. 已导入任务后，SQLite 是状态真相源。
3. Orchestrator 更新状态时，应生成 event，并可同步写一份状态摘要到 Markdown。
4. 如果人工改了已导入 Markdown 的 front matter：
   - 默认不自动覆盖 SQLite；
   - 进入 `needs_review` 或 `needs_manual_recovery`；
   - 由 Human/CodeX 明确选择重新导入或忽略。
5. 需要 `content_hash` / `frontmatter_hash` 字段检测导入后文件是否被改动。

建议新增字段：

```sql
content_hash TEXT,
frontmatter_hash TEXT,
imported_at TEXT,
last_synced_at TEXT
```

### 17.4 Hermes bridge 不能假设本地 Python 可直接调用会话内工具

计划里保留 Hermes 作为上层 control-plane / reviewer，但需要明确边界：

- `delegate_task` 是 Hermes 会话内工具，不是普通本地 Python 包可以直接 import 的 API。
- 如果 Python Orchestrator 作为独立进程运行，不能默认直接调用 Hermes 会话内 subagent。
- 可选实现路径：
  1. 把 Hermes 作为上层人工/agent 控制面，由 Hermes 读取 SQLite/MCP 并调度；
  2. 通过 Hermes CLI/API/cronjob 形式暴露一个可调用入口；
  3. 通过 `hermes_bridge` adapter 显式定义输入、输出、权限和超时；
  4. 先不实现 `hermes_bridge`，把它列为 Phase D5 或 Phase F。

因此 Phase D 成功标准应避免依赖 Hermes bridge；可先用 OpenCode + Codex + Claude + fake reviewer 跑通，再接 Hermes。Hermes 的优势是人机监督、规划、review 和 MCP client，而不是替代 Python Orchestrator 的确定性状态机。

### 17.5 MCP 需要区分“本项目 MCP server”和“Hermes MCP client 配置”

本方案要实现的是 `ai-workgroup-mcp` server。若要让 Hermes 使用它，需要额外配置 Hermes MCP client，例如：

```yaml
mcp_servers:
  aiwg:
    command: "python"
    args: ["-m", "aiwg.mcp.server", "--config", "D:/AIGroup/ai-workgroup-orchestrator/aiwg.yaml"]
    timeout: 120
    connect_timeout: 60
```

注意：

- Hermes 发现 MCP server 通常发生在启动时，新增/修改 server 后需要重启 Hermes。
- stdio MCP server 不应继承全量敏感环境变量；需要显式传入必要 env。
- MCP server 工具名在 Hermes 里会带前缀，例如 `mcp_aiwg_list_inbox`。
- Sampling 默认能力要谨慎；不可信 server 应禁用或限制 sampling。

### 17.6 Codex CLI adapter 需要写明前置条件

Codex CLI 不是简单 subprocess 即可稳定运行，需要在 adapter 章节补充：

- 必须在 git repository 内运行。
- 需要 Codex CLI 安装和认证可用。
- 优先使用 one-shot：`codex exec "..."`。
- 长任务用后台进程并记录 stdout/stderr。
- 不建议依赖 Codex Desktop Automation 作为后台 runner。
- 若 Codex CLI 需要 TTY/PTY，Windows 下要提前 spike；否则 adapter 只能做 read-only planning/review。

### 17.7 Runner 进程管理需要更具体

需要补充通用 runner 执行规范：

- 每次运行创建独立 `run_id`。
- stdout/stderr 写入 artifact 文件，不把大日志塞进 SQLite。
- 超时后必须 kill 整个进程树，而不是只 kill 父进程。
- Windows 下要验证子进程、shell、编码、cwd、路径空格。
- runner 必须显式声明是否允许 shell、是否允许写文件、是否可能弹交互确认。
- 对真实外部 agent，默认 `enabled=false`，必须显式打开。

### 17.8 Artifact 存储需要单独设计

建议新增：

```text
docs/ai-workgroup/state/artifacts/
  runs/<run-id>/
    prompt.md
    stdout.log
    stderr.log
    report.md
    changed-files.json
    verification-<n>-stdout.log
    verification-<n>-stderr.log
```

SQLite 只保存路径和摘要，不保存大文本。

### 17.9 安全与密钥处理需要补强

除 Human Gate 外，还需要补：

- `.env`、密钥、token、cookie、认证文件永远列入 forbidden patterns。
- stdout/stderr/event payload 写入前做 secret redaction。
- 禁止 agent 把环境变量完整打印到报告。
- 禁止把私有 token 写入 Markdown 审计文件。
- 对外部命令使用 allowlist，特别是删除、reset、deploy、push、merge。
- 路径必须 canonicalize，防止 `../`、符号链接、Windows drive path 绕过 `allowed_files`。

### 17.10 Phase A 应更小、更可验收

Phase A 已按可验收粒度拆成 5 个里程碑：

```text
A0: pyproject + aiwg CLI skeleton + aiwg.yaml safe defaults + doctor
A1: front matter parser + schema validator，复用旧 fixtures
A2: SQLite init/migrations + import/list + legacy audit_only report
A3: atomic claim + fake adapter + event log happy path
A4: read-only status/dashboard endpoint，不写业务状态
```

Phase A 成功标准应加入真实命令：

```bash
python -m pytest -q
python -m aiwg.cli doctor --config aiwg.yaml
python -m aiwg.cli init-db --config aiwg.yaml
python -m aiwg.cli import-inbox --config aiwg.yaml --agent Fake --dry-run
python -m aiwg.cli legacy-audit --config aiwg.yaml
python -m aiwg.cli run-once --config aiwg.yaml --agent Fake
python -m aiwg.cli list-tasks --config aiwg.yaml --status done
```

约束：Phase A 不接真实 AI writer，不 push，不 merge，不部署，不自动 `git init`；如果需要初始化 git，只生成 Human decision card。

### 17.11 测试计划要加入数据库和 Windows 专项

补充测试：

- 同一 Markdown 重复导入不会生成重复 task。
- 两个进程同时 claim 同一个 task，只有一个成功。
- SQLite locked/busy 时会重试或失败成明确状态。
- Windows 中文路径、空格路径、UTF-8 无 BOM。
- `allowed_files` 对 Windows `\` 与 `/` 的归一化。
- symlink/junction 绕过测试。
- stdout/stderr 中 secret redaction。
- timeout kill process tree。

### 17.12 Dashboard 不应直接改业务状态

Human dashboard 可以创建 decision card，但不应直接把任务改成 done 或 approved，除非走同一套 API/gate。

建议：

- Dashboard 所有写操作都调用 Orchestrator API/MCP 工具。
- Human 点击按钮只生成 `from-Human_to-CodeX` decision 或调用受限 action。
- Dashboard 操作全部写 event，支持审计。

### 17.13 需要增加“能力评分与路由”但推迟实现

v2 目标里提到指标，但还没落到路由策略。建议先记录、后使用：

- agent/task_type 成功率。
- 平均耗时。
- 打回率。
- acceptance 失败率。
- 人类介入率。
- 成本估算。

Phase E 后再让 scheduler 根据历史表现选择 agent；Phase A-D 不做自动智能路由，避免过早复杂化。

---

## 18. 自审后的修订优先级

建议按以下顺序补进实现计划或直接进入 Phase A：

1. **立即补**：`aiwg.yaml` safe defaults、SQLite WAL/index/constraint、Markdown/SQLite 同步策略、Safety Switch。
2. **Phase A 前补**：A0-A4 细分、CLI 命令清单、pyproject/依赖选择、legacy migration `audit_only` 报告。
3. **Phase B 前补**：artifact 存储、secret redaction、Windows 路径和进程树 kill、Git Steward dry-run。
4. **Phase C 前补**：MCP server 与 Hermes MCP client 的边界和配置样例，所有 MCP 写操作必须走 gate。
5. **Phase D 前补**：Codex/Claude/OpenCode/Hermes bridge adapter 的认证、TTY、权限弹窗、git repo 前置条件；真实 agent 默认 disabled。
6. **Phase E 前补**：dashboard 只通过 API/MCP 受限 action 写状态、能力评分只记录不自动路由。

自审结论：**方案主方向正确，但需要先把配置、数据库约束、同步策略、安全总开关、Git/PR/CI 版本流和旧消息迁移策略补细；否则一开始写 Python skeleton 时容易把路径、状态、agent 配置和不可逆操作混在一起，后面会返工甚至影响真实业务项目。Hermes 有能力承担上层规划、审查和协调，但稳定的执行真相源必须仍是 Python + SQLite + deterministic gates。**
