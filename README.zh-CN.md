# AI Workgroup Orchestrator

一个用于协调多个 AI 编程智能体的本地优先控制平面。核心目标是把“靠聊天记忆协作”改造成“靠版本化配置、SQLite 状态、机器可读任务契约和确定性 gate 协作”。

English: [README.md](README.md)

## 项目定位

AI Workgroup Orchestrator 是一个实验性的多 AI 编程协作控制层。它不是直接追求“无人值守乱跑”，而是先把协作机制做稳：

- 角色和 workflow 用版本化配置定义。
- 任务、checkpoint、审计、写入 gate 使用 SQLite ledger 管理。
- 智能体之间尽量通过机器可读状态交接，而不是只靠自然语言报告。
- MCP 默认只开放只读工具。
- 写入受保护业务仓之前先经过 dry-run write gate。
- Git Steward 先只做 worktree / commit / PR / review flow 的 dry-run 规划。
- 运行前置 role health、policy、budget、preflight 检查。
- 明确保护业务仓根目录，避免把控制面 artifact 写进业务项目。

默认策略非常保守：真实 agent、业务仓写入、GitHub 写操作、PR mutation、push、merge、deploy、CodeX Automation 修改全部关闭，除非后续经过明确 gate 和授权。

## 当前安全状态

默认配置适合本地查看和测试：

- `allow_write=false`
- `allow_real_agents=false`
- `allow_real_adapter_dispatch=false`
- `allow_real_process_execution=false`
- `allow_push=false`
- `allow_merge=false`
- `allow_deploy=false`
- `allow_modify_codex_automations=false`
- MCP 只暴露 4 个只读工具：
  - `status`
  - `list_tasks`
  - `get_task`
  - `recent_events`

这个项目目前是“安全控制平面基础设施”，不是已经可以全自动接管业务项目的成品系统。

## 目录结构

```text
aiwg/                         Python 包与 CLI
tests/aiwg/                   测试套件
docs/guides/                  阶段指南和操作说明
docs/plans/                   设计方案和实施计划
docs/examples/                示例 workflow / 配置
docs/ai-workgroup/            协议、拓扑、workflow 契约
scripts/ai-workgroup/         早期 PowerShell 辅助脚本
aiwg.yaml                     安全示例配置
pyproject.toml                包元数据和 pytest 配置
```

运行态 inbox、SQLite ledger、生成的 acceptance artifact、临时 worktree、日志、缓存和 quarantine 文件默认不提交到 Git。

## 快速开始

依赖：

- Python 3.11+
- Git
- Windows、macOS 或 Linux。当前主要在 Windows 上开发和验证。

安装：

```powershell
cd D:\AIGroup\ai-workgroup-orchestrator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,mcp]"
```

运行检查：

```powershell
python -m aiwg.cli doctor --config aiwg.yaml --project-root .
python -m pytest -q tests/aiwg
python -m aiwg.mcp.server --config aiwg.yaml --list-tools
```

Linux/macOS 激活虚拟环境：

```bash
source .venv/bin/activate
```

## 配置

仓库里的 `aiwg.yaml` 是安全示例配置。使用前请把 `protected_target_roots` 改成你自己的受保护业务仓路径。

示例：

```yaml
protected_target_roots:
  - D:/example/protected-business-repo
policy:
  safe_mode: true
  allow_write: false
  allow_real_agents: false
  allow_real_adapter_dispatch: false
  allow_real_process_execution: false
  allow_push: false
  allow_merge: false
  allow_deploy: false
```

安全关键开关必须是严格布尔值。系统会拒绝 `"false"`、`0`、`null`、错误结构等模糊输入，避免 Python truthiness 误判。

## 开发流程

本项目采用 TDD + 安全 gate 的推进方式。新增能力通常应按以下顺序：

1. 先写 planning-only artifact。
2. 写 RED 测试，证明当前不安全行为存在。
3. 做最小 GREEN 实现。
4. 跑 targeted regression。
5. 跑完整 `tests/aiwg`。
6. 跑 doctor。
7. 检查 MCP surface。
8. 扫描受保护业务仓边界。
9. 更新 review / acceptance artifact。

任何真实写入、真实 agent 执行、GitHub 写操作或部署能力，都必须继续放在明确的 dry-run 和 policy gate 之后。

## MCP

当前 MCP server 只读。查看工具：

```powershell
python -m aiwg.mcp.server --config aiwg.yaml --list-tools
```

预期输出：

```text
status
list_tasks
get_task
recent_events
```

目前不开放 mutation tools。

## 安全模型

核心边界：

- 没有 write gate 决策，不写受保护业务仓。
- 没有 policy、preflight、approval gate，不派发真实 agent。
- 默认不使用 GitHub 写 API、不评论 PR、不 push、不 merge、不 deploy、不修改 CodeX Automation。
- evidence / audit artifact 必须留在 orchestrator 目录。
- protected target roots fail-closed。
- policy switch 必须使用严格布尔值。

安全问题请看 [SECURITY.md](SECURITY.md)。

## 贡献

请看 [CONTRIBUTING.md](CONTRIBUTING.md)。建议保持改动小、测试明确、边界写清楚。

## 许可证

MIT License，见 [LICENSE](LICENSE)。

许可证以英文原文为准；中文说明仅便于理解，不构成法律文本。
