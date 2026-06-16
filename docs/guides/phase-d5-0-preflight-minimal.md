# Phase D5.0 — Minimal preflight slice

## Status

`D5.0` implements the first minimal D5-preflight control-plane slice and is currently `completed_ready_for_independent_review`.

This slice is intentionally below the real-execution line. It records deterministic Orchestrator-side evidence for schema, snapshot, artifact provenance, CLI, and dashboard only.

## Scope

D5.0 includes:

- SQLite schema migration `7 -> 8`.
- `d5_preflight_runs` table with fail-closed `CHECK(... = 0)` constraints for forbidden execution/mutation booleans.
- `d5_artifact_provenance` table with fail-closed constraints proving artifacts are under the Orchestrator root and not under the target root.
- `aiwg.d5_preflight.evaluate_d5_preflight()` API for fake/dry-run snapshot generation.
- `aiwg.cli d5-preflight --dry-run` command.
- Dashboard/status snapshot key `d5_preflight`.
- Guide and acceptance artifact under the Orchestrator artifact root.

D5.0 explicitly defers these D5 plan items to D5.1:

- `budget_preflight`
- `checkpoint_lease_heartbeat_stale_recovery`
- `external_review_fixture_ingest`

## Non-goals and hard boundaries

D5.0 does **not**:

- enable real agents;
- expose MCP mutation tools;
- call GitHub write APIs;
- create PR comments;
- perform PR mutation;
- create fix tasks;
- write the protected AIVideoTrans business repository;
- modify CodeX Automations;
- push, merge, or deploy;
- consume or read secret values;
- run destructive commands.

The protected target repository remains read-only context only:

```text
D:/example/protected-business-repo
```

All D5.0 mutable state is Orchestrator-side only:

```text
D:/AIGroup/ai-workgroup-orchestrator/docs/ai-workgroup/state/tasks.sqlite
D:/AIGroup/ai-workgroup-orchestrator/docs/ai-workgroup/state/artifacts/phase-d5-preflight/
```

## Implementation files

Created:

- `aiwg/d5_preflight.py`
- `tests/aiwg/preflight/test_d50_preflight.py`
- `docs/guides/phase-d5-0-preflight-minimal.md`
- `docs/ai-workgroup/state/artifacts/phase-d5-preflight/acceptance.json`

Modified:

- `aiwg/state/database.py`
- `aiwg/cli.py`
- `aiwg/dashboard/status.py`
- schema migration expectation tests under `tests/aiwg/state/`, `tests/aiwg/runners/`, `tests/aiwg/git/`, `tests/aiwg/dashboard/`, and `tests/aiwg/review/`

## SQLite schema v8

D5.0 raises:

```text
SCHEMA_VERSION = 8
MIGRATION_NAME = phase_d5_preflight_minimal
```

New migration row:

```text
(8, phase_d5_preflight_minimal)
```

### `d5_preflight_runs`

The run table stores one dry-run evidence row per D5.0 preflight invocation.

Forbidden fields are constrained to false at the SQLite layer:

```text
ready_for_real_agent_execution = 0
ready_for_protected_business_repository_write = 0
target_writes_performed = 0
mcp_mutation_tools_exposed = 0
github_write_api_called = 0
pr_comment_performed = 0
pr_mutation_performed = 0
created_fix_tasks = 0
codex_automation_modified = 0
git_push_performed = 0
git_merge_performed = 0
git_deploy_performed = 0
real_agents_started = 0
real_processes_started = 0
```

The D5.0 targeted test asserts attempts to insert unsafe values fail with `sqlite3.IntegrityError`.

### `d5_artifact_provenance`

The provenance table stores checksummed artifact metadata and proves path boundaries:

```text
under_orchestrator_root = 1
under_target_root = 0
```

The D5.0 targeted test asserts attempts to record target-root artifacts fail with `sqlite3.IntegrityError`.

## Snapshot contract

The D5.0 snapshot uses:

```text
schema_version = aiwg.d5_preflight_result.v1
phase = D5.0
d5_scope = D5.0-minimal
status = passed_dry_run | blocked | failed
```

Expected safe snapshot values:

```text
dry_run=true
fake_only=true
read_only=true
mutation_actions=[]
ready_for_real_agent_execution=false
ready_for_protected_business_repository_write=false
target_writes_performed=false
mcp_mutation_tools_exposed=false
github_write_api_called=false
pr_comment_performed=false
pr_mutation_performed=false
created_fix_tasks=false
codex_automation_modified=false
git_push_performed=false
git_merge_performed=false
git_deploy_performed=false
real_agents_started=false
real_processes_started=false
```

Unsafe policy toggles produce a blocked snapshot while keeping all mutation/execution flags false. The D5.0 targeted tests cover `policy.allow_real_agents=true` and `policy.allow_push=true`.

## CLI

Required dry-run invocation:

```bash
python -m aiwg.cli d5-preflight \
  --config aiwg.yaml \
  --workflow-id apf-preview-funnel \
  --target-root D:/example/protected-business-repo \
  --dry-run \
  --json
```

Missing `--dry-run` fails closed:

```text
d5-preflight: error=--dry-run is required for D5.0 fake/dry-run preflight
```

## Dashboard

`python -m aiwg.cli status --config aiwg.yaml --json` now includes a read-only `d5_preflight` key when a D5.0 run exists.

Text rendering includes:

```text
D5 preflight
- status=passed_dry_run | scope=D5.0-minimal | dry_run=true | fake_only=true | ready_for_real_agent_execution=false | target_writes_performed=false | mcp_mutation_tools_exposed=false
```

## TDD evidence

RED was confirmed before implementation:

```text
ModuleNotFoundError: No module named 'aiwg.d5_preflight'
```

GREEN targeted result:

```text
python -m pytest tests/aiwg/preflight/test_d50_preflight.py -q
5 passed in 0.90s
```

Related regression result:

```text
python -m pytest tests/aiwg/preflight/test_d50_preflight.py tests/aiwg/state/test_a2_sqlite_import.py tests/aiwg/state/test_b0_schema_hardening.py tests/aiwg/runners/test_b7_operator_preflight_approval.py tests/aiwg/git/test_d4_git_steward_dry_run.py tests/aiwg/dashboard/test_d42_role_health_contract.py tests/aiwg/topology/test_d44_topology_workflow_contract.py tests/aiwg/review/test_d43_external_review_gate.py -q
65 passed in 7.25s
```

Full suite result:

```text
python -m pytest -q
278 passed in 54.23s
```

Doctor:

```text
AIWG doctor: OK
```

The only retained warning is unchanged and non-blocking for this dry-run/read-only phase:

```text
[WARN] not a git repository
```

MCP surface remains read-only with exactly four tools:

```text
status
list_tasks
get_task
recent_events
```

## CLI/status smoke evidence

D5.0 CLI dry-run JSON produced:

```text
status=passed_dry_run
phase=D5.0
d5_scope=D5.0-minimal
dry_run=True
fake_only=True
ready_for_real_agent_execution=False
ready_for_protected_business_repository_write=False
target_writes_performed=False
mcp_mutation_tools_exposed=False
github_write_api_called=False
pr_comment_performed=False
pr_mutation_performed=False
created_fix_tasks=False
codex_automation_modified=False
git_push_performed=False
git_merge_performed=False
git_deploy_performed=False
real_agents_started=False
real_processes_started=False
artifact_under_orchestrator=true
artifact_under_target=false
```

Status dashboard JSON produced:

```text
d5_preflight_present=True
status=passed_dry_run
phase=D5.0
d5_scope=D5.0-minimal
dry_run=True
fake_only=True
ready_for_real_agent_execution=False
target_writes_performed=False
mcp_mutation_tools_exposed=False
github_write_api_called=False
pr_mutation_performed=False
codex_automation_modified=False
```

## Post-doc verification requirements

Before marking D5.0 ready for CodeX review, rerun:

```bash
python -m json.tool docs/ai-workgroup/state/artifacts/phase-d5-preflight/acceptance.json
python -m pytest tests/aiwg/preflight/test_d50_preflight.py -q
python -m aiwg.cli doctor --config aiwg.yaml --project-root .
python -m aiwg.mcp.server --list-tools
```

Also scan touched docs/code for secret-like assignments and verify AIVideoTrans has no D5.0 artifacts, SQLite DBs, workflow files, provenance records, or D5.0 marker files.

## Next step

D5.0 must pass independent review before CodeX review. Only after CodeX passes D5.0 should D5.1 be considered. D5.1 should still remain fake/dry-run/preflight and may cover budget preflight, checkpoint lease/heartbeat/stale classification, and external review fixture ingest.
