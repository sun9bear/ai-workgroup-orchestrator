# AIWG D5-preflight Implementation Plan

> **For Hermes:** Use `subagent-driven-development` only after this plan is accepted. Implement task-by-task with RED-GREEN-TDD and two-stage review. Do **not** enable real execution.

**Goal:** Build a D5-preflight dry-run/fake readiness layer that proves AIWG can evaluate adapter runtime readiness, budget constraints, checkpoint lease/heartbeat/stale recovery, artifact provenance, and external review read-adapter evidence before any real agent, GitHub write, PR mutation, MCP mutation, or protected business repository write is allowed.

**Architecture:** Add one read-mostly/preflight aggregation layer on top of the existing D3/D4/D4.4 control plane. The layer may write **orchestrator-only** SQLite/audit evidence under `D:/AIGroup/ai-workgroup-orchestrator`, but it must not write `D:/example/protected-business-repo`, start real processes, call GitHub write APIs, comment on PRs, expose MCP mutation tools, or modify CodeX Automations. Every D5-preflight snapshot must end with `ready_for_real_agent_execution=false` until a later explicitly approved phase changes policy.

**Tech Stack:** Python 3.11, SQLite, pytest, YAML/JSON config and artifacts, existing AIWG CLI/dashboard modules.

---

## 0. Non-negotiable safety boundary

D5-preflight is **not D5 real execution**.

Keep these false in code, YAML, JSON snapshots, docs, tests, and acceptance:

```text
allow_write=false
allow_real_agents=false
allow_external_agents=false
allow_real_adapter_dispatch=false
allow_real_process_execution=false
allow_push=false
allow_merge=false
allow_deploy=false
allow_secret_access=false
allow_modify_codex_automations=false
allow_destructive_commands=false
ready_for_real_agent_execution=false
ready_for_protected_business_repository_write=false
mcp_mutation_tools_exposed=false
target_writes_performed=false
github_write_api_called=false
pr_comment_performed=false
pr_mutation_performed=false
created_fix_tasks=false
codex_automation_modified=false
```

Allowed D5-preflight mutations:

- orchestrator SQLite schema/data under `state_db` resolved from `D:/AIGroup/ai-workgroup-orchestrator/aiwg.yaml`;
- orchestrator artifacts under `docs/ai-workgroup/state/artifacts/phase-d5-preflight/`;
- orchestrator docs/tests/source files.

Forbidden D5-preflight actions:

- writing any file under `D:/example/protected-business-repo`;
- creating or modifying target worktrees;
- launching Claude Code, Codex, Hermes bridge, or any real adapter as an agent process;
- calling GitHub write APIs or adding PR comments;
- creating fix tasks from external review feedback;
- adding MCP mutation tools;
- modifying CodeX Automations.

---

## 1. Current context to preserve

Existing implemented slices:

- D3 workflow ledger:
  - `workflow_runs`
  - `workflow_steps`
  - `workflow_step_intents`
  - `workflow_step_outputs`
  - CLI: `workflow-plan`, `workflow-status`
- D4 Git Steward dry-run:
  - `git_worktree_proposals`
  - `git_commit_proposals`
  - `pr_gate_status`
  - CLI: `git-plan`, `pr-gate-status`
- D4.2 role health contract:
  - `agent_states`
  - `agent_health_events`
  - dashboard role cards
- D4.3 external review gate:
  - `external_review_sources`
  - `external_review_items`
  - `external_review_gate_snapshots`
  - CLI: `external-review-gate`
- D4.4 topology/workflow contract:
  - `docs/ai-workgroup/topology/aiwg.topology.v1.yaml`
  - `docs/ai-workgroup/workflows/apf-preview-funnel.workflow.v1.yaml`
  - `aiwg/topology.py`
  - `aiwg/workflow_contract.py`
  - CLI: `workflow-contract`

D4.4 CodeX review status:

```text
codex_review_passed
```

Known non-blocker:

```text
doctor warns: not a git repository
```

This warning must not be treated as permission to run Git writes.

---

## 2. Proposed D5-preflight deliverables

### New files

- `aiwg/d5_preflight.py`
- `tests/aiwg/preflight/test_d5_preflight.py`
- `docs/guides/phase-d5-preflight.md`
- `docs/ai-workgroup/state/artifacts/phase-d5-preflight/acceptance.json`

### Modified files

- `aiwg/state/database.py`
  - `SCHEMA_VERSION: 7 -> 8`
  - add D5-preflight tables and migration name
- `aiwg/cli.py`
  - add `d5-preflight` read/fake/dry-run command
- `aiwg/dashboard/status.py`
  - include latest D5-preflight snapshot in status output
- `aiwg/config.py`
  - add conservative `d5_preflight` config block if needed
- `aiwg.yaml`
  - keep all real/mutation switches false; add D5-preflight defaults only if required

### New SQLite tables, if implementation chooses schema-backed evidence

Use schema version 8.

```sql
CREATE TABLE IF NOT EXISTS d5_preflight_runs (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('passed_dry_run', 'blocked', 'failed')),
  dry_run INTEGER NOT NULL DEFAULT 1 CHECK(dry_run IN (0, 1)),
  fake_only INTEGER NOT NULL DEFAULT 1 CHECK(fake_only IN (0, 1)),
  ready_for_real_agent_execution INTEGER NOT NULL DEFAULT 0 CHECK(ready_for_real_agent_execution = 0),
  target_writes_performed INTEGER NOT NULL DEFAULT 0 CHECK(target_writes_performed = 0),
  mcp_mutation_tools_exposed INTEGER NOT NULL DEFAULT 0 CHECK(mcp_mutation_tools_exposed = 0),
  github_write_api_called INTEGER NOT NULL DEFAULT 0 CHECK(github_write_api_called = 0),
  pr_mutation_performed INTEGER NOT NULL DEFAULT 0 CHECK(pr_mutation_performed = 0),
  codex_automation_modified INTEGER NOT NULL DEFAULT 0 CHECK(codex_automation_modified = 0),
  artifact_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

```sql
CREATE TABLE IF NOT EXISTS d5_budget_preflight (
  id TEXT PRIMARY KEY,
  preflight_run_id TEXT NOT NULL,
  role TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  max_budget_usd REAL NOT NULL CHECK(max_budget_usd >= 0),
  requested_budget_usd REAL NOT NULL CHECK(requested_budget_usd >= 0),
  consumed_budget_usd REAL NOT NULL DEFAULT 0 CHECK(consumed_budget_usd = 0),
  status TEXT NOT NULL CHECK(status IN ('within_budget', 'budget_exceeded', 'missing_budget')),
  dry_run INTEGER NOT NULL DEFAULT 1 CHECK(dry_run IN (0, 1)),
  created_at TEXT NOT NULL,
  UNIQUE(preflight_run_id, role)
);
```

```sql
CREATE TABLE IF NOT EXISTS d5_checkpoint_lease_preflight (
  id TEXT PRIMARY KEY,
  preflight_run_id TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  checkpoint_id TEXT NOT NULL,
  role TEXT NOT NULL,
  lease_state TEXT NOT NULL CHECK(lease_state IN ('would_acquire', 'blocked', 'stale_detected', 'waiting_human')),
  real_lock_acquired INTEGER NOT NULL DEFAULT 0 CHECK(real_lock_acquired = 0),
  heartbeat_expected_seconds INTEGER NOT NULL CHECK(heartbeat_expected_seconds > 0),
  stale_after_seconds INTEGER NOT NULL CHECK(stale_after_seconds > heartbeat_expected_seconds),
  created_at TEXT NOT NULL,
  UNIQUE(preflight_run_id, workflow_id, checkpoint_id, role)
);
```

```sql
CREATE TABLE IF NOT EXISTS d5_artifact_provenance (
  id TEXT PRIMARY KEY,
  preflight_run_id TEXT NOT NULL,
  artifact_kind TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  artifact_sha256 TEXT NOT NULL,
  origin_component TEXT NOT NULL,
  workflow_id TEXT,
  step_id TEXT,
  intent_id TEXT,
  under_orchestrator_root INTEGER NOT NULL CHECK(under_orchestrator_root = 1),
  under_target_root INTEGER NOT NULL CHECK(under_target_root = 0),
  created_at TEXT NOT NULL,
  UNIQUE(preflight_run_id, artifact_path)
);
```

If adding all tables at once feels too large, split D5-preflight into D5.0 and D5.1. D5.0 may start with `d5_preflight_runs` + artifact provenance only, then add budget/lease tables in D5.1.

---

## 3. Implementation tasks

### Task 1: Add RED schema migration test for D5-preflight tables

**Objective:** Prove schema version 8 introduces D5-preflight evidence tables with fail-closed constraints.

**Files:**

- Test: `tests/aiwg/state/test_d5_preflight_schema.py`
- Modify later: `aiwg/state/database.py`

**Step 1: Write failing test**

Create `tests/aiwg/state/test_d5_preflight_schema.py`:

```python
from __future__ import annotations

import sqlite3

from aiwg.config import load_config
from aiwg.state.database import init_database


def test_d5_preflight_schema_tables_exist_and_fail_closed(tmp_path):
    project_root = tmp_path / "orchestrator"
    project_root.mkdir()
    config_path = project_root / "aiwg.yaml"
    config_path.write_text(
        "project_root: .\n"
        "state_db: docs/ai-workgroup/state/aiwg.sqlite3\n"
        "artifact_root: docs/ai-workgroup/state/artifacts\n",
        encoding="utf-8",
    )
    config = load_config(config_path)
    db_path = init_database(config=config, project_root=project_root)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 8
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "d5_preflight_runs" in tables
        assert "d5_artifact_provenance" in tables

        # Fail-closed: D5-preflight must not be able to store real execution true.
        try:
            conn.execute(
                """
                INSERT INTO d5_preflight_runs(
                  id, workflow_id, status, dry_run, fake_only,
                  ready_for_real_agent_execution, target_writes_performed,
                  mcp_mutation_tools_exposed, github_write_api_called,
                  pr_mutation_performed, codex_automation_modified,
                  created_at, updated_at
                ) VALUES (
                  'bad', 'wf', 'passed_dry_run', 1, 1,
                  1, 0, 0, 0, 0, 0,
                  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                )
                """
            )
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover
            raise AssertionError("ready_for_real_agent_execution=true must be rejected")
```

**Step 2: Run RED**

```bash
python -m pytest tests/aiwg/state/test_d5_preflight_schema.py -q
```

Expected: FAIL because schema version 8 / D5 tables do not exist yet.

**Step 3: Implement minimal schema**

Modify `aiwg/state/database.py`:

- `SCHEMA_VERSION = 8`
- append migration `(8, "phase_d5_preflight")`
- add table DDLs above
- update `MIGRATION_NAME` if the code expects it to reflect latest migration

**Step 4: Run GREEN**

```bash
python -m pytest tests/aiwg/state/test_d5_preflight_schema.py -q
```

Expected: PASS.

---

### Task 2: Add RED API test for D5-preflight snapshot fail-closed behavior

**Objective:** Create a deterministic preflight function that always returns dry-run/fake-only evidence and refuses unsafe config.

**Files:**

- Create: `tests/aiwg/preflight/test_d5_preflight.py`
- Create later: `aiwg/d5_preflight.py`

**Step 1: Write failing test**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from aiwg.config import load_config
from aiwg.d5_preflight import evaluate_d5_preflight


def write_config(project_root: Path) -> Path:
    config_path = project_root / "aiwg.yaml"
    config_path.write_text(
        "project_root: .\n"
        "state_db: docs/ai-workgroup/state/aiwg.sqlite3\n"
        "artifact_root: docs/ai-workgroup/state/artifacts\n"
        "allow_write: false\n"
        "allow_real_agents: false\n"
        "allow_push: false\n"
        "allow_merge: false\n"
        "allow_modify_codex_automations: false\n",
        encoding="utf-8",
    )
    return config_path


def test_d5_preflight_snapshot_is_fake_dry_run_and_blocks_real_execution(tmp_path):
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "target-business-repo"
    project_root.mkdir()
    target_root.mkdir()
    config_path = write_config(project_root)
    config = load_config(config_path)

    result = evaluate_d5_preflight(
        config=config,
        project_root=project_root,
        workflow_id="apf-preview-funnel",
        target_root=target_root,
        dry_run=True,
    )

    assert result["schema_version"] == "aiwg.d5_preflight_result.v1"
    assert result["dry_run"] is True
    assert result["fake_only"] is True
    assert result["ready_for_real_agent_execution"] is False
    assert result["target_writes_performed"] is False
    assert result["mcp_mutation_tools_exposed"] is False
    assert result["github_write_api_called"] is False
    assert result["pr_mutation_performed"] is False
    assert result["codex_automation_modified"] is False
    assert result["target_root"] == str(target_root.resolve())
    assert str(result["artifact_path"]).startswith(str(project_root.resolve()))
    assert not str(result["artifact_path"]).startswith(str(target_root.resolve()))
```

**Step 2: Run RED**

```bash
python -m pytest tests/aiwg/preflight/test_d5_preflight.py::test_d5_preflight_snapshot_is_fake_dry_run_and_blocks_real_execution -q
```

Expected: FAIL because `aiwg.d5_preflight` does not exist.

**Step 3: Minimal implementation**

Create `aiwg/d5_preflight.py` with:

- `evaluate_d5_preflight(...) -> dict[str, Any]`
- validation of safety switches
- artifact root resolution under orchestrator root
- target root retained as context only
- no subprocess, no GitHub, no target writes

Implementation sketch:

```python
FORBIDDEN_TRUE_CONFIG_KEYS = (
    "allow_write",
    "allow_real_agents",
    "allow_external_agents",
    "allow_real_adapter_dispatch",
    "allow_real_process_execution",
    "allow_push",
    "allow_merge",
    "allow_deploy",
    "allow_secret_access",
    "allow_modify_codex_automations",
    "allow_destructive_commands",
)


def _policy_denials(config: dict[str, object]) -> list[str]:
    return [key for key in FORBIDDEN_TRUE_CONFIG_KEYS if bool(config.get(key))]
```

Return status `blocked` if any forbidden config is true, but keep all mutation booleans false.

**Step 4: Run GREEN**

```bash
python -m pytest tests/aiwg/preflight/test_d5_preflight.py -q
```

Expected: PASS for the first test.

---

### Task 3: Add budget preflight without budget consumption

**Objective:** Verify requested per-role/per-workflow budget is within contract/config and records zero consumed cost.

**Files:**

- Modify: `tests/aiwg/preflight/test_d5_preflight.py`
- Modify: `aiwg/d5_preflight.py`
- Possibly modify: `docs/ai-workgroup/workflows/apf-preview-funnel.workflow.v1.yaml`

**Step 1: Add RED tests**

Add tests:

```python
def test_d5_budget_preflight_blocks_budget_exceeded_without_consuming_budget(tmp_path):
    # Arrange project/config with max_budget_usd=0.50, requested_budget_usd=0.75
    # Act evaluate_d5_preflight(...)
    # Assert status=blocked, budget.status=budget_exceeded,
    # consumed_budget_usd=0, ready_for_real_agent_execution=false
    ...


def test_d5_budget_preflight_accepts_within_budget_but_still_not_real_ready(tmp_path):
    # requested_budget_usd <= max_budget_usd
    # Assert budget.status=within_budget but ready_for_real_agent_execution remains false
    ...
```

**Step 2: Run RED**

```bash
python -m pytest tests/aiwg/preflight/test_d5_preflight.py -q
```

Expected: FAIL on missing budget fields.

**Step 3: Implement**

- Read budget from workflow contract first (`budget_policy.max_workflow_budget_usd`) or config fallback.
- Add result block:

```json
"budget_preflight": {
  "status": "within_budget | budget_exceeded | missing_budget",
  "requested_budget_usd": 0.0,
  "max_budget_usd": 0.5,
  "consumed_budget_usd": 0.0,
  "dry_run": true
}
```

- If writing SQLite evidence, insert `d5_budget_preflight` rows with `consumed_budget_usd=0`.

**Step 4: Verify**

```bash
python -m pytest tests/aiwg/preflight/test_d5_preflight.py -q
```

Expected: PASS.

---

### Task 4: Add checkpoint lease/heartbeat/stale preflight

**Objective:** Model would-acquire lease decisions and stale recovery classification without actually acquiring real runner locks or launching agents.

**Files:**

- Modify: `tests/aiwg/preflight/test_d5_preflight.py`
- Modify: `aiwg/d5_preflight.py`

**Step 1: Add RED tests**

```python
def test_d5_checkpoint_lease_preflight_records_would_acquire_without_real_lock(tmp_path):
    # Assert lease_state=would_acquire and real_lock_acquired=false.
    ...


def test_d5_checkpoint_lease_preflight_classifies_stale_without_resetting_task(tmp_path):
    # Seed stale checkpoint/heartbeat fixture or previous preflight row.
    # Assert lease_state=stale_detected and no task is reset to ready.
    ...
```

**Step 2: Run RED**

```bash
python -m pytest tests/aiwg/preflight/test_d5_preflight.py -q
```

Expected: FAIL because lease fields do not exist.

**Step 3: Implement**

- Use workflow contract checkpoints from D4.4.
- For each checkpoint, classify:
  - `would_acquire` if no stale/blocking context;
  - `waiting_human` if human gate boundary applies;
  - `stale_detected` if prior heartbeat is older than `stale_after_seconds`;
  - `blocked` if the checkpoint violates policy.
- Do **not** mutate `tasks.status` from stale back to ready.
- Do **not** launch runner process.
- If writing SQLite evidence, insert `d5_checkpoint_lease_preflight` rows only.

**Step 4: Verify**

```bash
python -m pytest tests/aiwg/preflight/test_d5_preflight.py -q
```

Expected: PASS.

---

### Task 5: Add artifact provenance registry for orchestrator-only artifacts

**Objective:** Every D5-preflight artifact must have a sha256 and path-boundary evidence proving it lives under the orchestrator root, not the target root.

**Files:**

- Modify: `tests/aiwg/preflight/test_d5_preflight.py`
- Modify: `aiwg/d5_preflight.py`

**Step 1: Add RED tests**

```python
def test_d5_artifact_provenance_rejects_target_root_artifact_path(tmp_path):
    # Pass/force an artifact path under target_root.
    # Assert ValueError or blocked result before creating files.
    ...


def test_d5_artifact_provenance_records_sha256_for_orchestrator_artifact(tmp_path):
    # Run preflight.
    # Read artifact path, compute sha256, compare with result['artifact_provenance'].
    ...
```

**Step 2: Run RED**

```bash
python -m pytest tests/aiwg/preflight/test_d5_preflight.py -q
```

Expected: FAIL because provenance not implemented.

**Step 3: Implement**

- Write D5-preflight report under:

```text
docs/ai-workgroup/state/artifacts/phase-d5-preflight/preflight-<safe-id>.json
```

- Compute sha256 with Python `hashlib.sha256`.
- Resolve paths with `Path.resolve()`.
- Fail if artifact path is not under orchestrator root.
- Fail if artifact path is under target root.

**Step 4: Verify**

```bash
python -m pytest tests/aiwg/preflight/test_d5_preflight.py -q
```

Expected: PASS.

---

### Task 6: Add external review read-adapter preflight from local fixture only

**Objective:** Prove D5 can consume external gate evidence through a read-only adapter contract without PR mutation, comments, or GitHub writes.

**Files:**

- Modify: `tests/aiwg/preflight/test_d5_preflight.py`
- Modify: `aiwg/d5_preflight.py`
- Possibly create fixture: `tests/fixtures/d5_external_review_read_adapter/approved.json`

**Step 1: Add RED tests**

```python
def test_d5_external_review_read_adapter_accepts_read_only_fixture(tmp_path):
    # Fixture contains read_only=true, mutation_actions=[], gate_state=approved.
    # Assert source ingested/classified as read-only evidence and no PR mutation flags are true.
    ...


def test_d5_external_review_read_adapter_blocks_write_capable_fixture(tmp_path):
    # Fixture contains read_only=false or mutation_actions=['comment_pr'].
    # Assert preflight status=blocked and pr_comment_performed=false.
    ...
```

**Step 2: Run RED**

```bash
python -m pytest tests/aiwg/preflight/test_d5_preflight.py -q
```

Expected: FAIL because fixture adapter not implemented.

**Step 3: Implement**

- Read local JSON fixture only.
- Do not call network.
- Do not use GitHub CLI/API.
- Normalize into D4.3 external review gate terms:
  - `source_type`
  - `gate_state`
  - `feedback_category`
  - `read_only`
  - `mutation_actions`
- If `read_only=false`, malformed `mutation_actions_json`, or non-empty mutation actions, block the D5-preflight result.
- Keep snapshot fields:

```json
"external_review_read_adapter": {
  "read_only": true,
  "network_called": false,
  "github_write_api_called": false,
  "pr_comment_performed": false,
  "mutation_actions": []
}
```

**Step 4: Verify**

```bash
python -m pytest tests/aiwg/preflight/test_d5_preflight.py -q
```

Expected: PASS.

---

### Task 7: Add CLI command `d5-preflight`

**Objective:** Provide a real CLI smoke path for D5-preflight without enabling real execution.

**Files:**

- Modify: `aiwg/cli.py`
- Modify: `tests/aiwg/preflight/test_d5_preflight.py`

**Step 1: Add RED CLI test**

```python
def test_d5_preflight_cli_json_is_dry_run_and_read_only(tmp_path):
    # Run: python -m aiwg.cli d5-preflight --config <config> --workflow-id apf-preview-funnel --target-root <target> --dry-run --json
    # Assert JSON flags all false, status is passed_dry_run or blocked, exit code 0 for valid dry-run.
    ...
```

**Step 2: Run RED**

```bash
python -m pytest tests/aiwg/preflight/test_d5_preflight.py::test_d5_preflight_cli_json_is_dry_run_and_read_only -q
```

Expected: FAIL because command does not exist.

**Step 3: Implement CLI**

Add parser:

```python
d5_preflight_parser = subcommands.add_parser(
    "d5-preflight",
    help="Run D5 fake/dry-run preflight readiness aggregation; no real agents or target writes.",
)
_add_common_config_arg(d5_preflight_parser)
d5_preflight_parser.add_argument("--workflow-id", required=True)
d5_preflight_parser.add_argument("--target-root", required=True)
d5_preflight_parser.add_argument("--dry-run", action="store_true")
d5_preflight_parser.add_argument("--json", action="store_true")
d5_preflight_parser.set_defaults(func=_d5_preflight_command)
```

The command must reject missing `--dry-run`.

**Step 4: Verify**

```bash
python -m pytest tests/aiwg/preflight/test_d5_preflight.py -q
python -m aiwg.cli d5-preflight --config aiwg.yaml --workflow-id apf-preview-funnel --target-root D:/example/protected-business-repo --dry-run --json
```

Expected:

- tests pass;
- CLI exits 0 for valid preflight;
- output includes `dry_run=true`, `fake_only=true`, `ready_for_real_agent_execution=false`, `target_writes_performed=false`.

---

### Task 8: Add dashboard/status read-only display

**Objective:** Surface latest D5-preflight status in the dashboard without creating DB files from read-only status calls.

**Files:**

- Modify: `aiwg/dashboard/status.py`
- Modify: `tests/aiwg/dashboard/test_d5_preflight_dashboard.py`

**Step 1: Add RED dashboard test**

```python
def test_status_dashboard_includes_latest_d5_preflight_without_mutation(tmp_path):
    # Seed a D5 preflight run in SQLite.
    # Call get_status_snapshot.
    # Assert snapshot['d5_preflight'] exists and all safety booleans are false.
    # Assert read-only status does not create new target files.
    ...
```

**Step 2: Run RED**

```bash
python -m pytest tests/aiwg/dashboard/test_d5_preflight_dashboard.py -q
```

Expected: FAIL because dashboard does not include D5-preflight.

**Step 3: Implement**

- Add `_read_latest_d5_preflight(conn)` using existing read-only connection.
- Add `d5_preflight` to `_empty_snapshot` as `None`.
- Render text section:

```text
D5 preflight
- status=passed_dry_run | ready_for_real_agent_execution=false | target_writes_performed=false
```

**Step 4: Verify**

```bash
python -m pytest tests/aiwg/dashboard/test_d5_preflight_dashboard.py -q
python -m aiwg.cli status --config aiwg.yaml --json
```

Expected: dashboard JSON contains `d5_preflight` and still exposes no mutation actions.

---

### Task 9: Add guide and acceptance artifacts

**Objective:** Document D5-preflight scope, verification commands, and non-goals before CodeX review.

**Files:**

- Create: `docs/guides/phase-d5-preflight.md`
- Create: `docs/ai-workgroup/state/artifacts/phase-d5-preflight/acceptance.json`

**Step 1: Write guide**

The guide must state:

- D5-preflight is fake/dry-run/preflight only;
- D5-preflight writes only orchestrator evidence;
- target repo zero-write evidence is required;
- no real agents, no MCP mutation, no GitHub write, no PR mutation, no CodeX Automation changes;
- how to run CLI/test verification.

**Step 2: Write acceptance JSON**

Initial status after implementation and self-verification:

```json
{
  "schema_version": "aiwg.phase_acceptance.v1",
  "phase": "D5-preflight",
  "status": "completed_ready_for_independent_review",
  "safety": {
    "dry_run_only": true,
    "fake_only": true,
    "ready_for_real_agent_execution": false,
    "target_writes_performed": false,
    "mcp_mutation_tools_exposed": false,
    "github_write_api_called": false,
    "pr_mutation_performed": false,
    "codex_automation_modified": false
  }
}
```

**Step 3: Verify JSON**

```bash
python -m json.tool docs/ai-workgroup/state/artifacts/phase-d5-preflight/acceptance.json
```

Expected: valid JSON.

---

### Task 10: Required verification before independent review

**Objective:** Gather milestone evidence using the dry-run review checklist.

Run from `D:/AIGroup/ai-workgroup-orchestrator`:

```bash
python -m pytest tests/aiwg/preflight/test_d5_preflight.py -q
python -m pytest tests/aiwg/state/test_d5_preflight_schema.py tests/aiwg/preflight/test_d5_preflight.py tests/aiwg/dashboard/test_d5_preflight_dashboard.py tests/aiwg/topology/test_d44_topology_workflow_contract.py tests/aiwg/review/test_d43_external_review_gate.py -q
python -m pytest -q
python -m aiwg.cli doctor --config aiwg.yaml --project-root .
python -m aiwg.mcp.server --list-tools
python -m aiwg.cli d5-preflight --config aiwg.yaml --workflow-id apf-preview-funnel --target-root D:/example/protected-business-repo --dry-run --json
python -m aiwg.cli status --config aiwg.yaml --json
python -m json.tool docs/ai-workgroup/state/artifacts/phase-d5-preflight/acceptance.json
```

Expected:

- targeted tests pass;
- related regression passes;
- full suite passes;
- doctor remains OK except known non-blocking git warning;
- MCP tools remain exactly:

```text
status
list_tasks
get_task
recent_events
```

- CLI snapshot shows all safety/mutation booleans false;
- acceptance JSON validates.

### Task 11: Boundary and secret scans before review

**Objective:** Prove D5-preflight did not leak secrets or write target repo artifacts.

Use Hermes search tools rather than ad hoc shell greps.

Secret scan targets:

- `aiwg/d5_preflight.py`
- `tests/aiwg/preflight/test_d5_preflight.py`
- `docs/guides/phase-d5-preflight.md`
- `docs/ai-workgroup/state/artifacts/phase-d5-preflight/acceptance.json`

Pattern:

```text
(?i)(api[_-]?key|secret|password|token|passwd)\s*[:=]\s*['\"]?[^'\"\s]{6,}
```

Expected: `0 matches`.

Target repo scan:

Path: `D:/example/protected-business-repo`

Search for:

- `*d5*preflight*`
- `*phase-d5*`
- `*d5_preflight*`
- content: `D5-preflight|phase-d5-preflight|d5_preflight|ready_for_real_agent_execution`

Expected: `0 matches`.

---

## 4. Independent review / CodeX gate

After tasks 1-11 pass:

1. Run independent review with explicit instructions:
   - D5-preflight only;
   - no real agents;
   - no target writes;
   - verify schema constraints fail closed;
   - verify MCP tools remain read-only;
   - verify target repo scan is 0.
2. If independent review finds issues:
   - write RED test first;
   - fix;
   - rerun targeted/regression/full where feasible;
   - update guide/acceptance;
   - request post-fix independent review.
3. Only after post-fix independent review passes, mark:

```text
completed_ready_for_codex_review
```

4. Ask CodeX to review.
5. Do **not** proceed to real execution after CodeX passes D5-preflight unless the user explicitly authorizes a separate D5-real milestone.

---

## 5. Risks and mitigations

### Risk: D5-preflight becomes real execution by accident

Mitigation:

- schema `CHECK(... = 0)` constraints for safety booleans;
- tests that try to insert or return true values;
- CLI requires `--dry-run`;
- no subprocess launch code in `aiwg/d5_preflight.py`.

### Risk: external review adapter mutates PRs

Mitigation:

- first adapter reads only local fixtures;
- no GitHub CLI/API call;
- dirty source rows with `read_only=false` or non-empty mutation actions block.

### Risk: target repo gets orchestrator artifacts

Mitigation:

- all artifact paths resolve under orchestrator root;
- provenance table records `under_orchestrator_root=1`, `under_target_root=0`;
- target repo scan is an acceptance criterion.

### Risk: budget ledger implies spend authorization

Mitigation:

- `consumed_budget_usd=0` in D5-preflight;
- budget preflight is advisory/blocking only;
- `ready_for_real_agent_execution=false` even when within budget.

### Risk: lease/heartbeat preflight resets stale work

Mitigation:

- D5-preflight may classify stale state but must not reset tasks to `ready`;
- recovery policy remains future/human-gated.

---

## 6. Suggested milestone slice

Recommended first implementation slice:

```text
D5.0 = schema + d5_preflight snapshot + artifact provenance + CLI + dashboard + docs/acceptance
```

Defer if needed:

```text
D5.1 = richer budget rows + checkpoint lease heartbeat fixtures + external review fixture ingest
```

However, if time permits, implement all D5-preflight pieces in one pass because they are still fake/dry-run and testable.

---

## 7. Completion criteria

D5-preflight is ready for CodeX only when all are true:

- targeted D5 tests pass;
- related regression passes;
- full suite passes;
- doctor OK, with only known non-blocking git warning if still present;
- MCP tools remain exactly 4 read-only tools;
- CLI `d5-preflight --dry-run --json` works;
- dashboard includes D5-preflight read-only snapshot;
- acceptance JSON validates;
- secret scan has 0 matches;
- business repo scan has 0 D5 markers/artifacts;
- independent review passes after any fixes;
- acceptance status is `completed_ready_for_codex_review`.

Until then, keep D5-preflight below the real execution line.
