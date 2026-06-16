# Contributing / 贡献指南

Thank you for considering a contribution. This project is a safety-oriented control plane, so small, well-tested changes are preferred over broad rewrites.

感谢你考虑贡献。本项目是偏安全控制面的基础设施，因此优先接受范围清晰、测试充分的小改动，而不是一次性大重构。

## Development Rules

- Keep real agents, protected writes, GitHub mutation, push, merge, deploy, and CodeX Automation changes disabled unless a reviewed phase explicitly enables them.
- Prefer RED -> GREEN TDD for behavior changes.
- Add or update tests under `tests/aiwg/`.
- Run targeted tests and, when practical, the full AIWG suite before submitting.
- Keep generated runtime state out of git.
- Do not commit secrets, local credentials, private business data, inbox messages, runtime SQLite files, or quarantine artifacts.

## 开发规则

- 除非某个已审核阶段明确启用，否则不要打开真实 agent、业务仓写入、GitHub 写操作、push、merge、deploy 或 CodeX Automation 修改。
- 行为变更优先使用 RED -> GREEN 的 TDD 流程。
- 新增或更新 `tests/aiwg/` 下的测试。
- 提交前至少跑相关 targeted tests；条件允许时跑完整 AIWG suite。
- 生成的运行态文件不要提交到 Git。
- 不要提交密钥、本地凭据、业务私有数据、inbox 消息、SQLite 运行库或 quarantine artifact。

## Useful Commands

```powershell
python -m aiwg.cli doctor --config aiwg.yaml --project-root .
python -m pytest -q tests/aiwg
python -m aiwg.mcp.server --config aiwg.yaml --list-tools
```

## Pull Request Checklist

- [ ] The change has a clear scope.
- [ ] Tests cover the behavior change.
- [ ] `doctor` passes.
- [ ] MCP surface remains read-only unless the PR explicitly documents and gates a reviewed mutation surface.
- [ ] Protected business repository paths are not written by tests or scripts.
- [ ] No secrets or local runtime files are committed.

## PR 检查清单

- [ ] 改动范围清楚。
- [ ] 行为变更有测试覆盖。
- [ ] `doctor` 通过。
- [ ] MCP 仍保持只读，除非 PR 明确说明并 gate 了已审核的 mutation surface。
- [ ] 测试或脚本没有写受保护业务仓。
- [ ] 没有提交密钥或本地运行态文件。
