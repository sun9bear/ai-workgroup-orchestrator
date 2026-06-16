# AI Workgroup Orchestrator v2 Phase C Read-only MCP Server Implementation Plan

> **For Hermes:** Use test-driven-development skill to implement this plan task-by-task. Keep AIVideoTrans as read-only context only; do not modify any files outside `D:/AIGroup/ai-workgroup-orchestrator`.

**Goal:** Add a minimal Phase C MCP read-only access layer so external AI tools can inspect the SQLite task system without parsing Markdown queues or mutating state.

**Architecture:** Split Phase C into a pure Python read-only tool layer and an optional MCP stdio server wrapper. The tool layer is fully testable without the `mcp` package; the server wrapper should fail clearly when the optional MCP SDK is missing.

**Tech Stack:** Python 3.11, SQLite `mode=ro`, existing `aiwg.dashboard.status` snapshot code, optional `mcp` Python SDK.

---

## Current Baseline

Verified on 2026-06-06:

```text
python -m pytest -q -> 127 passed
python -m aiwg.cli doctor -> AIWG doctor: OK
python -m aiwg.cli status -> read-only SQLite status, one Fake done task
```

Safety defaults remain enabled:

```text
safe_mode=true
allow_real_agents=false
allow_real_adapter_dispatch=false
allow_real_process_execution=false
allow_write=false
allow_push=false
allow_merge=false
allow_deploy=false
allow_modify_codex_automations=false
```

APF3b direct-write artifacts are quarantined and not accepted into mainline:

```text
docs/ai-workgroup/quarantine/2026-06-06-hermes-apf3b-direct-write/manifest.json
file_count=13
status=quarantined_not_accepted
```

## Non-goals

- Do not modify `D:/example/protected-business-repo`.
- Do not restore quarantined APF3b files into AIVideoTrans.
- Do not expose MCP write tools yet.
- Do not claim/update/approve/cancel tasks through MCP.
- Do not start Claude/Codex/OpenCode/Hermes real agents.
- Do not change Git, PR, merge, deploy, or CodeX automation settings.

## Tool Contract

Phase C read-only tools:

```text
status(config_path='aiwg.yaml', recent_events=10, task_limit=50, status_filter=None, agent=None)
list_tasks(config_path='aiwg.yaml', status_filter=None, agent=None, limit=50)
get_task(config_path='aiwg.yaml', task_id)
recent_events(config_path='aiwg.yaml', limit=10)
```

Common guarantees:

- Return JSON-safe dictionaries/lists.
- Include `capabilities.read_only=true` and `mutation_actions=[]` where appropriate.
- Open existing SQLite via read-only path only through existing status/dashboard code.
- Missing DB returns empty read-only data, not initialization.
- Unknown task returns `found=false` without mutation.
- No function writes events, artifacts, approvals, or task rows.

## Task C0: Baseline + Plan

**Files:**

- Create: `docs/plans/2026-06-06-aiwg-phase-c-readonly-mcp-server-plan.md`

**Verification:**

```bash
python -m pytest -q
python -m aiwg.cli doctor
python -m aiwg.cli status
```

Expected: tests pass; doctor OK; status remains read-only.

## Task C1: RED tests for read-only tool layer

**Files:**

- Create: `tests/aiwg/mcp/test_c0_read_only_tools.py`

**Test cases:**

1. `status_tool` mirrors existing read-only snapshot and does not mutate DB.
2. `list_tasks_tool` returns filtered task summaries from SQLite.
3. `get_task_tool` returns one task by id and `found=false` for missing ids.
4. `recent_events_tool` returns newest events and does not mutate DB.
5. Missing database does not get created.

**RED command:**

```bash
python -m pytest -q tests/aiwg/mcp/test_c0_read_only_tools.py
```

Expected: fail because `aiwg.mcp.tools` does not exist.

## Task C2: GREEN implementation for read-only tool layer

**Files:**

- Create: `aiwg/mcp/__init__.py`
- Create: `aiwg/mcp/tools.py`

**Implementation notes:**

- Reuse `load_config` and `resolve_project_root`.
- Reuse `get_status_snapshot` for SQLite read-only behavior.
- Do not call `init_database`, `import_inbox`, `run_once`, or any approval functions.
- Keep write action list empty.

**GREEN command:**

```bash
python -m pytest -q tests/aiwg/mcp/test_c0_read_only_tools.py
```

Expected: all new tests pass.

## Task C3: RED tests for server contract

**Files:**

- Create: `tests/aiwg/mcp/test_c1_mcp_server_contract.py`

**Test cases:**

1. `python -m aiwg.mcp.server --help` exits 0 and lists read-only tool names.
2. `aiwg.mcp.server` exposes `READ_ONLY_TOOL_NAMES` with exactly `status`, `list_tasks`, `get_task`, `recent_events`.
3. If the optional MCP SDK is missing, `main()` returns a clear non-zero error for server start, without mutation.

**RED command:**

```bash
python -m pytest -q tests/aiwg/mcp/test_c1_mcp_server_contract.py
```

Expected: fail because `aiwg.mcp.server` does not exist.

## Task C4: GREEN MCP stdio server shell

**Files:**

- Create: `aiwg/mcp/server.py`
- Optional modify: `pyproject.toml` to add `[project.optional-dependencies].mcp = ["mcp>=..."]`

**Implementation notes:**

- Provide CLI:

```bash
python -m aiwg.mcp.server --config D:/AIGroup/ai-workgroup-orchestrator/aiwg.yaml
```

- `--help` must work even if MCP SDK is not installed.
- Start mode should check/import MCP SDK and fail clearly if missing.
- Register only read-only tools.
- No write tools are registered.

## Task C5: Verification

Run:

```bash
python -m pytest -q
python -m aiwg.cli doctor
python -m aiwg.cli status
python -m aiwg.mcp.server --help
python - <<'PY'
from pathlib import Path
root = Path('D:/example/protected-business-repo')
forbidden = [
 'src/services/anonymous_preview_probe.py',
 'src/services/anonymous_preview_compliance.py',
 'src/services/anonymous_preview_upload_handler.py',
 'tests/test_anonymous_preview_probe.py',
 'tests/test_anonymous_preview_compliance.py',
 'tests/test_anonymous_preview_upload_handler.py',
]
remaining = [p for p in forbidden if (root / p).exists()]
print('aivideotrans_remaining_direct_files', len(remaining))
PY
```

Expected:

```text
all tests pass
doctor OK
status read-only
server help exits 0
AIVideoTrans direct APF3b files remain absent
```

## Rollback Strategy

If C1/C2 fails unexpectedly:

- Remove only Phase C files:
  - `aiwg/mcp/`
  - `tests/aiwg/mcp/`
  - this plan file if necessary
- Do not touch AIVideoTrans.
- Do not delete quarantine artifacts.
- Keep `aiwg.yaml` safety switches unchanged.

## Handoff to CodeX / Claude Code

If delegated later, implementers may work only under:

```text
D:/AIGroup/ai-workgroup-orchestrator/aiwg/mcp/**
D:/AIGroup/ai-workgroup-orchestrator/tests/aiwg/mcp/**
D:/AIGroup/ai-workgroup-orchestrator/docs/plans/2026-06-06-aiwg-phase-c-readonly-mcp-server-plan.md
```

Any attempt to modify AIVideoTrans, real adapter dispatch, CodeX automations, Git push/merge/deploy, or production configuration must be treated as scope violation.
