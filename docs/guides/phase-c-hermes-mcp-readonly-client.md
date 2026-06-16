# Phase C3: Hermes MCP read-only client setup

本指南说明如何让 Hermes Agent 通过 MCP 只读访问 AI Workgroup Orchestrator v2 的 SQLite 控制面。

范围限定：只读查询 Orchestrator 状态，不修改 AIVideoTrans，不启用真实 agent，不开放写工具。

## 当前阶段

- Phase A/B20: 控制面安全基线基本完成。
- Phase C0/C1: `aiwg.mcp` read-only tool layer + server shell 已完成。
- Phase C2: MCP SDK optional dependency + stdio smoke 已完成。
- Phase C3: 提供 Hermes MCP client 配置样例和外部接入说明。
- Phase D/E: 未开始。

## 暴露工具

AIWG MCP server 只暴露以下 read-only tools：

- `status`
- `list_tasks`
- `get_task`
- `recent_events`

Hermes discovery 后的工具名应为：

- `mcp_aiwg_readonly_status`
- `mcp_aiwg_readonly_list_tasks`
- `mcp_aiwg_readonly_get_task`
- `mcp_aiwg_readonly_recent_events`

## 配置样例

配置样例位于：

```text
docs/examples/hermes-mcp-aiwg-readonly.yaml
```

将其中 `mcp_servers` block 合并到：

```text
~/.hermes/config.yaml
```

示例核心内容：

```yaml
mcp_servers:
  aiwg_readonly:
    command: "python"
    args:
      - "-m"
      - "aiwg.mcp.server"
      - "--config"
      - "D:/AIGroup/ai-workgroup-orchestrator/aiwg.yaml"
    cwd: "D:/AIGroup/ai-workgroup-orchestrator"
    timeout: 120
    connect_timeout: 60
    sampling:
      enabled: false
```

如果你的 Hermes 配置已有其它 MCP server，请只合并 `aiwg_readonly` 条目，不要覆盖整个 `mcp_servers`。

## 本地验证命令

在 `D:/AIGroup/ai-workgroup-orchestrator` 中运行：

```bash
python -m aiwg.cli doctor
python -m aiwg.mcp.server --require-sdk-check-only
python -m aiwg.mcp.server --list-tools
python -m pytest -q tests/aiwg/mcp
```

预期：

```text
AIWG doctor: OK
MCP SDK available
status
list_tasks
get_task
recent_events
```

## Hermes 侧验证

修改 `~/.hermes/config.yaml` 后，重启 Hermes。也可以在支持的会话中使用：

```text
/reload-mcp
```

然后检查：

```bash
hermes mcp list
hermes mcp test aiwg_readonly
```

成功后，Hermes 应能调用：

```text
mcp_aiwg_readonly_status
mcp_aiwg_readonly_list_tasks
mcp_aiwg_readonly_get_task
mcp_aiwg_readonly_recent_events
```

## 安全边界

Phase C3 仍然保持以下边界：

- 不修改 AIVideoTrans 业务项目。
- 不恢复 APF3b quarantine 产物。
- 不启用真实 agent。
- 不开放 MCP 写操作。
- 不执行 push / merge / deploy。
- 不修改 CodeX Automations。
- 不在 MCP 配置中放 secrets、tokens、API keys、credentials 或 connection strings。

AIWG MCP server 当前只提供 read-only status surface。它不应该承担任务领取、状态更新、批准、真实 agent 调度或业务仓库写入。

## 故障排查

### MCP SDK missing

如果看到 SDK 缺失：

```bash
python -m aiwg.mcp.server --require-sdk-check-only
```

先安装项目 extra 或约束依赖：

```bash
python -m pip install -e ".[mcp]"
```

本项目的 `mcp` extra 显式约束：

```toml
mcp = [
  "mcp>=1.0",
  "starlette<0.47",
  "sse-starlette>=1.6.1,<3",
]
```

这样避免 `pip install mcp` 在共享环境里拉取不兼容的 `starlette` major。

### Tools not visible in Hermes

检查：

1. `~/.hermes/config.yaml` 是否包含 `mcp_servers.aiwg_readonly`。
2. `command` 是否可执行。
3. `cwd` 是否是 `D:/AIGroup/ai-workgroup-orchestrator`。
4. `args` 是否包含 `-m aiwg.mcp.server --config D:/AIGroup/ai-workgroup-orchestrator/aiwg.yaml`。
5. Hermes 是否已经重启或执行 `/reload-mcp`。

### Adapter readiness stale

`status` 中可能显示 adapter readiness stale。它是运行时状态，不代表可以启用真实 agent。真实 agent 仍需后续 Phase D 的单独审批、preflight 和 human authorization。

## 后续阶段

建议下一阶段仍不启用真实 agent，而是进入 Phase C4：

- 外部 MCP client smoke 文档化。
- dashboard/status 中强化 stale readiness warning。
- 设计受限写操作 gate，但不实现默认写工具。
