# Phase D1 Dry-Run-Only Write-Gate Evaluator Plan

> D1 implements executable evaluation only. It must not perform protected business-repository writes.

## Goal

Build a deterministic write-gate evaluator that accepts a candidate write intent plus an approval envelope draft and returns either:

```text
deny
dry_run_only
```

It writes Orchestrator-side audit artifacts plus the Phase D2 SQLite audit/idempotency/rollback ledger, but never modifies AIVideoTrans or starts real agents.

## Scope

Allowed Orchestrator-side changes:

```text
aiwg/write_gate.py
aiwg/cli.py
tests/aiwg/write_gate/test_d1_dry_run_write_gate_evaluator.py
docs/plans/2026-06-06-aiwg-phase-d1-dry-run-write-gate-evaluator-plan.md
docs/guides/phase-d1-dry-run-write-gate-evaluator.md
docs/ai-workgroup/state/artifacts/phase-d1-dry-run-write-gate/**
docs/ai-workgroup/done/Hermes/**
```

Forbidden in D1:

```text
no protected business repository writes
no real agents
no real adapter dispatch
no real process execution
no MCP mutation tools
no push / merge / deploy
no credential/token/secret access
no CodeX Automations modification
```

## Required behavior

The evaluator must deny when:

1. approval envelope is missing;
2. envelope is expired;
3. a candidate path is outside `approved_paths`;
4. a candidate path matches `forbidden_paths`;
5. rollback plan is missing;
6. verification commands are missing;
7. idempotency key is duplicated.

The evaluator may return `dry_run_only` only when all D1 checks pass.

## Audit artifact

Every evaluation writes an audit artifact under:

```text
docs/ai-workgroup/state/artifacts/phase-d1-dry-run-write-gate/
```

Audit artifacts record:

```text
schema_version
phase
decision
reasons
duplicate_idempotency_key
target_writes_performed=false
real_agents_started=false
mcp_mutation_tools_exposed=false
candidate summary
approval envelope summary
safety switches
secret handling policy
```

Raw target file content is not recorded. Credential-like values must be `[REDACTED]`.

## CLI

D1 exposes a CLI evaluator:

```bash
python -m aiwg.cli write-gate-dry-run \
  --config aiwg.yaml \
  --project-root . \
  --candidate docs/ai-workgroup/state/artifacts/phase-d1-dry-run-write-gate/sample-candidate-intent.json \
  --envelope docs/ai-workgroup/state/artifacts/phase-d1-dry-run-write-gate/sample-approval-envelope.json \
  --json
```

The CLI returns `0` when evaluation completes, including deny results, because deny is a valid gate decision.

## Tests

D1 follows RED/GREEN:

```bash
python -m pytest -q tests/aiwg/write_gate/test_d1_dry_run_write_gate_evaluator.py
```

Regression:

```bash
python -m pytest -q tests/aiwg/write_gate tests/aiwg/mcp tests/aiwg/dashboard/test_c4_stale_readiness_warning.py
python -m pytest -q
python -m aiwg.cli doctor
hermes mcp test aiwg_readonly
```

## Acceptance

D1 is complete only when:

```text
D1 tests pass
D0 tests remain green
full suite passes
doctor OK
Hermes MCP still exposes only 4 read-only tools
AIVideoTrans APF3b direct source/test count remains 0
real writes remain disabled
```
