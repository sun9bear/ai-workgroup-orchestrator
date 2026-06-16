# Phase D0 Controlled Write-Gate Design Plan

> **For Hermes:** Use test-driven-development for any executable gate code in later phases. D0 itself is design only.

**Goal:** Define the minimum controlled write-gate contract that future phases must satisfy before any protected business-repository write or real-agent execution can be considered.

**Architecture:** D0 creates a design-only contract and acceptance artifact. The contract is deny-by-default and can only produce `deny` or `dry_run_only` decisions in D0. It does not add executable write paths, MCP mutation tools, real-agent dispatch, or AIVideoTrans file changes.

**Tech Stack:** Python tests, JSON acceptance artifact, Markdown guide, existing AIWG policy/config/doctor surfaces.

---

## Scope

Design only.

D0 may create or update files under:

```text
D:/AIGroup/ai-workgroup-orchestrator/docs/plans/
D:/AIGroup/ai-workgroup-orchestrator/docs/guides/
D:/AIGroup/ai-workgroup-orchestrator/docs/ai-workgroup/state/artifacts/
D:/AIGroup/ai-workgroup-orchestrator/tests/aiwg/write_gate/
D:/AIGroup/ai-workgroup-orchestrator/docs/ai-workgroup/done/Hermes/
```

D0 must not modify AIVideoTrans:

```text
D:/example/protected-business-repo/src/**
D:/example/protected-business-repo/tests/**
D:/example/protected-business-repo/docs/**
```

D0 must not enable real agents, real adapter dispatch, real process execution, push, merge, deploy, or CodeX Automations modification.

## Non-goals

```text
no real writes
no real agents
no MCP mutation tools
no protected business repository writes
no AIVideoTrans changes
no push / merge / deploy
no secret/token access
```

## Current inherited baseline

Phase C read-only MCP is completed and ready_for_review / conditionally accepted.

Phase C exposed MCP business tools remain:

```text
status
list_tasks
get_task
recent_events
```

D0 must preserve that MCP surface.

## D0 write-gate contract

The future write gate must be deny-by-default.

D0 allowed decision values:

```text
deny
dry_run_only
```

The string `allow` is intentionally not a D0 decision value.

Before a later phase can introduce any real write decision, the following must be designed and tested:

1. approval envelope;
2. exact approved/forbidden path lists;
3. rollback plan artifact;
4. audit event schema;
5. idempotency key;
6. verification command list;
7. expiry timestamp;
8. phase-envelope match check;
9. operator identity;
10. protected repo boundary check.

## Approval envelope draft

A future approval envelope must include at least:

```yaml
phase: D1-or-later
task_id: stable-task-id
message_id: stable-message-id
operator: human-or-approved-controller
approved_paths: []
forbidden_paths: []
rollback_plan_path: docs/ai-workgroup/state/artifacts/.../rollback.md
verification_commands: []
expires_at: ISO-8601 timestamp
idempotency_key: stable-hash-or-uuid
```

D0 does not make this envelope executable. It only defines the required fields and blocks future work from skipping them.

## Task 1: D0 RED test

**Objective:** Capture the design-only write-gate acceptance contract before creating the artifact.

**Files:**

- Create: `tests/aiwg/write_gate/test_d0_controlled_write_gate_design.py`

**Run:**

```bash
python -m pytest -q tests/aiwg/write_gate/test_d0_controlled_write_gate_design.py
```

**Expected RED:** fails because `write-gate-design.json`, plan, and guide do not exist.

## Task 2: D0 GREEN artifact and guide

**Objective:** Create the minimum design-only artifact and guide.

**Files:**

- Create: `docs/ai-workgroup/state/artifacts/phase-d0-controlled-write-gate-design/write-gate-design.json`
- Create: `docs/guides/phase-d0-controlled-write-gate-design.md`
- Create: `docs/plans/2026-06-06-aiwg-phase-d0-controlled-write-gate-design-plan.md`

**Run:**

```bash
python -m pytest -q tests/aiwg/write_gate/test_d0_controlled_write_gate_design.py
```

**Expected GREEN:** all D0 design tests pass.

## Task 3: Regression verification

**Objective:** Prove D0 did not regress Phase C or safety gates.

**Run:**

```bash
python -m pytest -q tests/aiwg/write_gate/test_d0_controlled_write_gate_design.py tests/aiwg/mcp tests/aiwg/dashboard/test_c4_stale_readiness_warning.py
python -m pytest -q
python -m aiwg.cli doctor
hermes mcp test aiwg_readonly
```

Expected:

```text
D0 design tests pass
full suite passes
AIWG doctor: OK
MCP tools discovered: 4
```

## Task 4: Boundary rescan

**Objective:** Confirm D0 did not modify protected AIVideoTrans files.

Read-only checks:

```text
aivideotrans_apf3b_direct_source_test_remaining_count 0
aivideotrans_apf3b_named_non_pyc_non_worktree_count 0
```

Known `.pyc` residues may remain and are not cleaned in D0.

## Acceptance criteria

D0 is complete only when:

- D0 artifact declares `status=design_only`;
- D0 artifact declares `ready_for_real_agent_execution=false`;
- D0 artifact declares `ready_for_protected_business_repository_write=false`;
- D0 artifact declares `mcp_mutation_tools_exposed=false`;
- D0 contract decisions are only `deny` and `dry_run_only`;
- approval envelope, rollback plan, audit event, idempotency key, verification commands, and phase-envelope checks are required for later phases;
- all tests pass;
- doctor remains OK;
- MCP surface remains Phase C read-only;
- AIVideoTrans is not modified.

## Secret handling

No credentials, tokens, API keys, connection strings, or secrets are needed. If any future evidence contains such values, record only `[REDACTED]`.
