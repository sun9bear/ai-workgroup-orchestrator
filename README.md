# AI Workgroup Orchestrator

Deterministic control plane for coordinating AI coding agents with explicit gates, SQLite-backed state, and dry-run-first safety checks.

中文文档：[README.zh-CN.md](README.zh-CN.md)

## What This Project Does

AI Workgroup Orchestrator is an experimental local-first orchestration layer for multi-agent software development. It focuses on making collaboration between tools such as Codex, Claude Code, OpenCode, Hermes, and other coding agents more reliable by moving coordination out of chat memory and into deterministic contracts.

The project currently emphasizes:

- Versioned workflow and role contracts.
- SQLite-backed task, checkpoint, audit, and gate ledgers.
- Machine-readable state instead of natural-language-only handoffs.
- Read-only MCP status tools by default.
- Dry-run write gates before any protected repository write.
- Git Steward dry-run gates for worktree, commit, PR, and review flow planning.
- Runtime role health, policy, budget, and preflight checks.
- Protected target repository boundaries.

The default posture is deliberately conservative: real agents, protected writes, GitHub mutation, deployment, merge, push, and Codex Automation modification are disabled unless explicitly enabled by a future reviewed gate.

## Current Safety Status

By default, this repository is designed to be safe to inspect and test locally:

- `allow_write=false`
- `allow_real_agents=false`
- `allow_real_adapter_dispatch=false`
- `allow_real_process_execution=false`
- `allow_push=false`
- `allow_merge=false`
- `allow_deploy=false`
- `allow_modify_codex_automations=false`
- MCP exposes read-only tools only:
  - `status`
  - `list_tasks`
  - `get_task`
  - `recent_events`

This project is not yet a turnkey autonomous coding system. It is a control-plane foundation for building one safely.

## Repository Layout

```text
aiwg/                         Python package and CLI
tests/aiwg/                   Test suite
docs/guides/                  Phase guides and operating notes
docs/plans/                   Design and implementation plans
docs/examples/                Example workflows and configs
docs/ai-workgroup/            Protocol, topology, workflow contracts
scripts/ai-workgroup/         Legacy PowerShell helper scripts
aiwg.yaml                     Safe local example configuration
pyproject.toml                Package metadata and test config
```

Runtime inboxes, local ledgers, generated acceptance artifacts, temporary worktrees, logs, caches, and quarantine files are intentionally ignored by git.

## Quick Start

Requirements:

- Python 3.11+
- Git
- Windows, macOS, or Linux. Most development has been done on Windows.

Install:

```powershell
cd D:\AIGroup\ai-workgroup-orchestrator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,mcp]"
```

Run checks:

```powershell
python -m aiwg.cli doctor --config aiwg.yaml --project-root .
python -m pytest -q tests/aiwg
python -m aiwg.mcp.server --config aiwg.yaml --list-tools
```

On Linux/macOS, use the equivalent shell activation command:

```bash
source .venv/bin/activate
```

## Configuration

`aiwg.yaml` is checked in as a safe example. Update `protected_target_roots` for your own protected business repository before experimenting.

Example:

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

The validator intentionally rejects ambiguous values such as `"false"`, `0`, `null`, or malformed policy sections for safety-critical switches.

## Development Workflow

This project uses test-driven hardening. New capabilities should normally follow:

1. Planning-only artifact.
2. RED tests proving the unsafe behavior exists.
3. Minimal GREEN implementation.
4. Targeted regression tests.
5. Full `tests/aiwg` suite.
6. Doctor check.
7. MCP surface check.
8. Boundary scan for protected repositories.
9. Review artifact update.

Before proposing any real write, agent execution, GitHub mutation, or deployment behavior, keep the feature behind explicit dry-run and policy gates.

## MCP

The current MCP server is read-only. List exposed tools:

```powershell
python -m aiwg.mcp.server --config aiwg.yaml --list-tools
```

Expected output:

```text
status
list_tasks
get_task
recent_events
```

Mutation tools are intentionally not exposed yet.

## Security Model

Core boundaries:

- No protected repository writes without a write-gate decision.
- No real agent dispatch without policy, preflight, and approval gates.
- No GitHub write API, PR comment, push, merge, deploy, or CodeX Automation mutation by default.
- Evidence and audit artifacts must stay inside the orchestrator evidence directory.
- Protected target roots are fail-closed.
- Policy switches require literal booleans.

Please see [SECURITY.md](SECURITY.md) for reporting and review guidance.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Keep changes small, tested, and explicit about safety boundaries.

## License

MIT License. See [LICENSE](LICENSE).

The English license text is authoritative. A Chinese explanation is provided in the Chinese README for convenience only.
