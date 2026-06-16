# Security Policy / 安全策略

## Supported Status

This project is experimental. Security-sensitive execution surfaces are intentionally disabled by default.

本项目仍处于实验阶段。安全敏感的执行面默认关闭。

Default disabled surfaces include:

- Real agent execution
- Protected business repository writes
- MCP mutation tools
- GitHub write API and PR mutation
- Git push / merge / deploy
- Secret access
- CodeX Automation modification

## Reporting a Vulnerability

Please open a private security advisory on GitHub if available. If private advisory is not available, open a minimal public issue that does not include exploit details or secrets, and ask for a secure contact path.

如果发现安全问题，请优先使用 GitHub private security advisory。如果不可用，请开一个不包含利用细节和密钥的最小公开 issue，并请求安全联系方式。

## What Counts as Security-Sensitive

Please report:

- A path boundary bypass that writes evidence, state, or artifacts into a protected repository.
- A policy parser bug that treats `"false"`, `0`, `null`, or malformed config as safe.
- MCP mutation tools becoming exposed unexpectedly.
- A dry-run path performing real writes or external mutations.
- A way to start real agents, GitHub writes, deployment, or CodeX Automation changes without explicit gates.
- Secret leakage through logs, artifacts, reports, or acceptance files.

## 安全敏感问题示例

请报告：

- 路径边界绕过，导致 evidence、state、artifact 写入受保护业务仓。
- policy 解析错误，把 `"false"`、`0`、`null` 或错误结构当成安全值。
- MCP mutation tools 被意外暴露。
- dry-run 路径实际执行了写入或外部修改。
- 未经过明确 gate 就能启动真实 agent、GitHub 写操作、部署或 CodeX Automation 修改。
- 密钥通过日志、artifact、报告或 acceptance 文件泄漏。

## Disclosure Expectations

Please do not publish exploit steps until a fix is available. The project is small and local-first; coordinated disclosure gives maintainers time to patch without encouraging unsafe automation patterns.

请在修复发布前不要公开利用步骤。本项目规模较小且偏本地控制面，协调披露能避免鼓励不安全的自动化用法。
