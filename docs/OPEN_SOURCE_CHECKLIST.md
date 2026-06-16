# Open Source Checklist / 开源检查清单

## Release Boundary

This repository should publish the orchestrator source, tests, stable guides, plans, example configuration, and protocol documents.

本仓库适合发布 orchestrator 源码、测试、稳定指南、方案文档、示例配置和协议文档。

## Do Commit

- `aiwg/`
- `tests/aiwg/`
- `tests/fixtures/`
- `docs/guides/`
- `docs/plans/`
- `docs/examples/`
- Stable files under `docs/ai-workgroup/`, such as protocol, topology, workflow, and skills.
- `scripts/ai-workgroup/` legacy helpers, if they do not contain secrets.
- `README.md`, `README.zh-CN.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `LICENSE`.
- `pyproject.toml`, `uv.lock`, `.gitignore`, `aiwg.yaml`.

## Do Not Commit

- `.env` or credentials.
- `.venv/`, caches, `__pycache__/`, `.pytest_cache/`.
- `.codex_tmp/`, `.codegraph/`, `.codex_worktrees/`.
- `.hermes_*` local scratch files.
- `logs/`.
- `docs/ai-workgroup/state/`.
- `docs/ai-workgroup/inbox/`, `working/`, `done/`, `quarantine/`, `spikes/`.
- Runtime SQLite files.
- Protected business repository artifacts.

## Pre-Push Checks

```powershell
python -m aiwg.cli doctor --config aiwg.yaml --project-root .
python -m pytest -q tests/aiwg
python -m aiwg.mcp.server --config aiwg.yaml --list-tools
git status --short
```

Before pushing, inspect staged files:

```powershell
git diff --cached --name-only
```

确认没有运行态文件、密钥、本地业务仓数据或受保护路径 artifact 被 stage。
