# Phase D1 dry-run-only write-gate evaluator

## Status

Executable dry-run evaluator implemented and hardened through D2.4 pre-D3 closure.

```text
real writes: disabled
real agents: disabled
MCP mutation tools: not exposed
target business repository writes: none
allowed decisions: deny / dry_run_only
```

D1 is not a true write phase. It only evaluates candidate write intents and records Orchestrator-side audit artifacts.

## API

```python
from aiwg.write_gate import evaluate_write_gate_dry_run

result = evaluate_write_gate_dry_run(
    config=config,
    project_root=target_root,              # legacy target-context default only
    orchestrator_root=orchestrator_root,  # required unless config['orchestrator_root'] is set
    target_root=target_root,              # optional explicit business repo root
    candidate_intent=candidate,
    approval_envelope=envelope,
)
```

As of D2.1 the Python API fails closed with `orchestrator_root_required` when callers omit both the explicit `orchestrator_root` argument and `config['orchestrator_root']`. CLI callers may still use the config file directory as the Orchestrator root default before invoking the API. As of D2.2, calls also fail closed with `orchestrator_root_collides_with_target_root` before any artifact directory is created if the resolved Orchestrator root equals or is inside the resolved target/business root.

Result fields:

```text
decision
reasons
audit_artifact_path
duplicate_idempotency_key
target_writes_performed
```

`target_writes_performed` must always be `False` in D1.

## CLI

```bash
python -m aiwg.cli write-gate-dry-run --config aiwg.yaml --candidate candidate.json --envelope envelope.json --json
```

The CLI writes an audit artifact and exits `0` when evaluation completes. A `deny` decision is a valid completed evaluation, not a CLI failure.

D1.1 adds explicit deny exit-code semantics for automation pipelines:

```bash
python -m aiwg.cli write-gate-dry-run --config aiwg.yaml --candidate candidate.json --envelope envelope.json --json --fail-on-deny
```

With `--fail-on-deny`, a completed `deny` decision returns exit code `3` while still writing/printing the JSON decision and audit artifact path.

## D1.1 hardening requirements

D1.1 hardens the dry-run evaluator before D2:

```text
candidate.phase must be present and equal envelope.phase
candidate.task_id must equal envelope.task_id
candidate.message_id must equal envelope.message_id
envelope.phase must be D1
artifact_root must resolve under orchestrator_root/docs/ai-workgroup/state/artifacts
rollback_plan_path must resolve under Orchestrator artifacts
rollback plan schema_version must be aiwg.phase_d1_rollback_plan.v1
rollback plan phase/task_id/message_id must match envelope
rollback plan must state target_writes_performed=false
rollback plan must state protected_business_repository_write_performed=false
Windows drive absolute paths deny
UNC paths deny
path traversal denies
Windows reserved device path segments deny
symlink escape from target root denies when symlink is supported
```

Unsafe `artifact_root` values return `deny` and fall back to the safe Orchestrator artifact base for the audit artifact. They must not create audit directories in protected business repositories.

## Deny reasons

D1 denies for these core reasons:

```text
missing_approval_envelope
approval_envelope_expired
path_not_approved:<path>
forbidden_path:<path>
missing_rollback_plan
missing_verification_commands
duplicate_idempotency_key
missing_candidate_phase
candidate_envelope_phase_mismatch
candidate_envelope_task_id_mismatch
candidate_envelope_message_id_mismatch
unsupported_phase:<phase>
artifact_root_outside_orchestrator_artifacts
rollback_plan_outside_orchestrator_artifacts
invalid_rollback_plan_schema
absolute_write_path:<path>
unc_write_path:<path>
path_traversal:<path>
reserved_windows_device_path:<path>
candidate_path_escapes_target_root:<path>
colon_write_path:<path>
```

Additional structural reasons may include missing envelope fields or invalid candidate write paths. Fail-closed API/configuration errors include `orchestrator_root_required`, `orchestrator_root_collides_with_target_root`, `invalid_policy_shape`, `invalid_safety_switch_type:<switch>`, and `unsafe_safety_switch_enabled:<switch>`.

## Audit, idempotency, and rollback registry

Audit artifacts live under:

```text
docs/ai-workgroup/state/artifacts/phase-d1-dry-run-write-gate/
```

As of D2, idempotency and rollback artifact registration use the SQLite ledger:

```text
docs/ai-workgroup/state/artifacts/phase-d1-dry-run-write-gate/write-gate-ledger.sqlite
```

The legacy `idempotency-index.json` is no longer written by the evaluator. A repeated idempotency key returns `deny` with `duplicate_idempotency_key` using the SQLite `write_gate_idempotency.idempotency_key` primary key. As of D2.1, any legacy active file found at the artifact root is quarantined under `legacy/idempotency-index*.json` before evaluation so it cannot be mistaken for the active state source.

Audit artifacts are staged as `.audit-<id>.pending`, recorded in SQLite using the intended final `audit-*.json` path, and finalized only after SQLite commit. As of D2.2, each evaluator startup reconciles interrupted commits: if the ledger points to a missing final audit and the matching pending file still exists, the pending file is finalized. As of D2.3, reconcile first validates the exact pending payload schema and binds it back to the ledger row (`decision`, `reasons`, boolean duplicate flag, candidate summary, envelope summary, safety/secret flags); payloads with unexpected keys such as raw content, safety-switch tampering, malformed JSON, type-loose duplicate flags, or type-loose safety switch values such as numeric `0` are not finalized. Stale unreferenced `.pending` / `.pending.tmp` files older than the configured code threshold are removed before the next evaluation.

## Safety switches

D1 continues to require these false boundaries:

```text
allow_write=false
allow_real_agents=false
allow_real_adapter_dispatch=false
allow_real_process_execution=false
allow_push=false
allow_merge=false
allow_deploy=false
allow_modify_codex_automations=false
allow_secret_access=false
allow_network_write=false
allow_destructive_commands=false
```

As of D2.4, the evaluator validates config safety switches before any `dry_run_only` decision. Any configured switch in the set above must be a real JSON/Python boolean and must be `false`; `allow_write=true` or any other enabled safety switch returns `deny`, and type-loose values such as `"false"`, numeric `0`, numeric `1`, or `null` return `deny` with `invalid_safety_switch_type:<switch>`. A supplied `config.policy` must be a mapping; explicit `policy: null` returns `deny` with `invalid_policy_shape` instead of defaulting open. Audit artifacts record the sanitized dry-run safety boundary with every switch set to JSON `false`; the deny reason records the invalid or unsafe config source.

## MCP boundary

Phase C MCP business tools remain read-only:

```text
status
list_tasks
get_task
recent_events
```

No MCP mutation tools are added in D1.

## Protected repository boundary

D1 may reference protected repository paths in candidate intent summaries, but it must not write to them.

The resolved Orchestrator root must not equal or sit inside the target/business root. This collision guard runs before artifact directory creation so a misconfigured Orchestrator root cannot create audit, ledger, rollback, legacy, or pending files inside a protected business repository.

AIVideoTrans remains protected:

```text
D:/example/protected-business-repo
```

Any future transition beyond dry-run requires a later phase with tested approval envelope, rollback plan, audit chain, idempotency, and explicit human authorization.
