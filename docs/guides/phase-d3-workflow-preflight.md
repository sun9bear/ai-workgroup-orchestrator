# Phase D3 Workflow Ledger and Fake-Adapter Preflight

## Status

Implemented as a D3-preflight minimum slice. This phase remains dry-run-only and fake-adapter-only.

```text
ready_for_real_agent_execution=false
ready_for_protected_business_repository_write=false
mcp_mutation_tools_exposed=false
allow_write=false
target_writes_performed=false
```

## Scope

D3 adds a resumable workflow checkpoint ledger before any real agent or protected business repository write is allowed.

Implemented surfaces:

- SQLite schema migration `phase_d3_workflow_preflight` (`SCHEMA_VERSION=4`).
- Tables:
  - `workflow_runs`
  - `workflow_steps`
  - `workflow_step_intents`
  - `workflow_step_outputs`
- Python API:
  - `aiwg.workflow_preflight.plan_workflow_dry_run(...)`
  - `aiwg.workflow_preflight.get_workflow_status(...)`
- CLI:
  - `python -m aiwg.cli workflow-plan --dry-run ...`
  - `python -m aiwg.cli workflow-status --json ...`

## Safety boundary

D3 deliberately does **not** open any of the following:

- real agent execution
- real adapter process dispatch
- MCP mutation tools
- protected business repository writes
- push / merge / deploy
- secret access
- CodeX Automation modification

Every D3 fake workflow output records:

```json
{
  "fake_adapter_only": true,
  "real_agents_started": false,
  "target_writes_performed": false,
  "mcp_mutation_tools_exposed": false
}
```

Artifacts are constrained to the Orchestrator artifact tree only:

```text
<orchestrator_root>/docs/ai-workgroup/state/artifacts/workflows/<workflow_id>/<step_id>/fake-output.json
```

`target_root` is audit context only. It is never used as an output root. D3 validates mutable paths before SQLite init, directory creation, or fake output writes:

- `state_db` must resolve under `<orchestrator_root>/docs/ai-workgroup/state` and must not overlap any step `target_root`.
- `artifact_root` must resolve under `<orchestrator_root>/docs/ai-workgroup/state/artifacts`.
- workflow/step/fake-output artifact paths must not overlap any step `target_root` in either direction.

If any of those checks fail, D3 raises a fail-closed `ValueError` before creating target-side files.

## Execution ordering

For each step:

1. Validate/normalize workflow steps before opening output directories.
2. Validate `state_db` under the Orchestrator state root before calling `init_database()`.
3. Resolve the Orchestrator artifact base as `<orchestrator_root>/docs/ai-workgroup/state/artifacts`.
4. Check existing `workflow_id + step_id` idempotency before workflow-run upsert/start events.
5. Insert/update `workflow_runs` only after path/idempotency preflight passes.
6. Insert/update `workflow_steps` only if the existing step key matches.
7. Insert `workflow_step_intents` before any adapter output.
8. For D3, only write a fake output artifact.
9. Insert `workflow_step_outputs` linked to the intent.
10. Record events:
    - `workflow_run_started`
    - `workflow_step_intent_recorded`
    - `workflow_step_fake_output_written`
    - `workflow_run_completed`

Failure before output leaves the intent in SQLite without an output row, so resume can continue from the last successful checkpoint.

## Idempotency and resume

A succeeded `workflow_step_outputs.idempotency_key` is globally unique through a partial SQLite unique index.

Behavior:

- A repeated workflow with an already-succeeded step skips that step and does not redispatch it.
- A failed step with an existing intent but no output reuses the intent on resume.
- A different workflow using an already-succeeded idempotency key returns `duplicate_idempotency_key` and dispatches no fake step.
- The same `workflow_id + step_id` with a changed idempotency key returns `idempotency_key_mismatch`, dispatches no fake step, and does not rewrite existing `workflow_runs`, `events`, `workflow_steps`, `workflow_step_intents`, or `workflow_step_outputs` rows.

## Post-review blocker fixes

Independent D3 reviews found five closure blockers across two passes. All are fixed and covered by tests:

1. **Same-step idempotency mismatch fail-open**
   - Previous risk: rerunning an existing `workflow_id + step_id` with a new idempotency key could leave ledger rows inconsistent.
   - Fix: check the stored step before workflow-run upsert/start events; mismatch returns `idempotency_key_mismatch`, dispatches `0` steps, and preserves old run/event/step/intent/output ledger rows.
   - Test: `test_same_workflow_step_changed_idempotency_key_fails_closed_without_ledger_rewrite`.

2. **Misconfigured artifact root under target/business repo**
   - Previous risk: if `config.artifact_root` pointed into the target repo, fake artifacts could be written there.
   - Fix: resolve the Orchestrator artifact base independently as `<orchestrator_root>/docs/ai-workgroup/state/artifacts`; reject artifact roots outside that tree or overlapping a step `target_root` before writing anything.
   - Test: `test_misconfigured_artifact_root_under_target_root_fails_closed_before_writing`.

3. **Misconfigured state DB under target/business repo**
   - Previous risk: if `config.state_db` pointed into the target repo, D3 could create `tasks.sqlite` there before any artifact checks.
   - Fix: validate `state_db` under the Orchestrator state root and reject overlap with step `target_root` before calling `init_database()`.
   - Test: `test_misconfigured_state_db_under_target_root_fails_closed_before_writing`.

4. **Target root nested inside workflow/step artifact path**
   - Previous risk: if a step `target_root` was nested under the workflow/step artifact path, D3 could write `fake-output.json` inside the declared target root.
   - Fix: use bidirectional overlap checks for configured artifact base, workflow artifact root, step artifact root, and fake output path against every step `target_root`.
   - Test: `test_target_root_nested_under_step_artifact_path_fails_closed_before_writing`.

5. **Idempotency mismatch still rewrote run/events**
   - Previous risk: the mismatch branch preserved step/intent/output keys but rewrote `workflow_runs.status` and appended events.
   - Fix: detect mismatch before workflow-run upsert/start events; return mismatch without ledger writes.
   - Test: strengthened `test_same_workflow_step_changed_idempotency_key_fails_closed_without_ledger_rewrite`.

## CLI examples

Plan a single-step fake workflow:

```bash
python -m aiwg.cli workflow-plan \
  --config aiwg.yaml \
  --workflow-id D3-cli-wf \
  --step cli-gate \
  --idempotency-key D3-cli-key \
  --target-root D:/example/protected-business-repo \
  --dry-run \
  --json
```

Read workflow status:

```bash
python -m aiwg.cli workflow-status \
  --config aiwg.yaml \
  --workflow-id D3-cli-wf \
  --json
```

`workflow-plan` refuses to run without `--dry-run`.

## Tests

D3 targeted tests are in:

```text
tests/aiwg/workflows/test_d3_workflow_preflight.py
```

They cover:

- intent is written before fake output;
- fake output links to intent;
- no real agents are started;
- no business repository artifacts are written;
- failed step can resume from the last successful gate;
- duplicate idempotency key does not redispatch;
- same-step changed idempotency key fails closed without ledger rewrite;
- misconfigured artifact root under target root fails closed before writing;
- misconfigured state DB under target root fails closed before writing;
- target root nested under workflow/step artifact path fails closed before writing;
- CLI `workflow-plan --dry-run --json` and `workflow-status --json` expose the dry-run status.

Latest post-fix verification:

```text
D3 targeted: 8 passed in 0.77s
D3/state/regression slice: 29 passed in 3.92s
full suite: 234 passed in 45.64s
doctor: AIWG doctor: OK
MCP tools: status,list_tasks,get_task,recent_events only; mutation_marker_tools=
AIVideoTrans workflow/write-gate artifact counts: 0
```
