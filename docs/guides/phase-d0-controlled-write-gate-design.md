# Phase D0 controlled write-gate design

## Status

Design only.

Phase D0 defines a future controlled write-gate contract. It does not implement a write-capable runner, does not enable real agents, and does not expose MCP mutation tools.

```text
ready_for_real_agent_execution: no
ready_for_protected_business_repository_write: no
mcp_mutation_tools_exposed: no
AIVideoTrans modified: no
```

## Inherited Phase C boundary

Phase C read-only MCP is completed and remains the external control-plane surface.

Allowed MCP business tools:

```text
status
list_tasks
get_task
recent_events
```

No write, approve, dispatch, merge, deploy, or protected-business-repository-write tool is exposed.

## D0 decision model

D0 is deny-by-default.

Allowed D0 decisions:

```text
deny
dry_run_only
```

`allow` is not available in D0.

`dry_run_only` means a future gate may evaluate a hypothetical write candidate and produce an audit/report artifact, but it still must not modify protected business files.

## Required future approval envelope

A later phase must not introduce real writes until an approval envelope exists and is tested. Required fields:

```text
phase
task_id
message_id
operator
approved_paths
forbidden_paths
rollback_plan_path
verification_commands
expires_at
idempotency_key
```

The envelope must be tied to the active phase envelope and exact path scope. It must expire. It must include a rollback plan and verification commands before any write-capable decision can be considered in a future phase.

## Required audit chain

Future write-gate execution must emit durable events for:

```text
write_gate_evaluated
write_gate_denied
write_gate_dry_run_reported
future_write_gate_approved
future_write_started
future_write_completed
future_write_rolled_back
```

D0 only names this audit chain. It does not emit write events because no write execution exists in D0.

## Rollback requirements

A future write approval must include a rollback plan artifact with:

```text
target paths
pre-write hashes or absence markers
restore instructions
verification commands
operator approval reference
```

No future protected write should be accepted without this rollback plan.

## Safety rules

D0 preserves:

```text
allow_write=false
allow_real_agents=false
allow_real_adapter_dispatch=false
allow_real_process_execution=false
allow_push=false
allow_merge=false
allow_deploy=false
allow_modify_codex_automations=false
```

Do not enable real agents.

D0 rule: do not modify AIVideoTrans.

Do not modify AIVideoTrans.

Do not expose MCP mutation tools.

Do not read or write credentials. Any credential-like value in future evidence must be `[REDACTED]`.

## Traceability

Phase C summary:

```text
docs/guides/phase-c-readonly-mcp-acceptance-summary.md
```

Phase C E2E report:

```text
docs/ai-workgroup/done/Hermes/2026-06-06T151412_from-Hermes_to-Human_type-report_task-AIWG-Phase-C7-Hermes-MCP-E2E-smoke.md
```

D0 artifact:

```text
docs/ai-workgroup/state/artifacts/phase-d0-controlled-write-gate-design/write-gate-design.json
```
