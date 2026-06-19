# AIWG D5.3.13 Adapter Readiness Gate Config Error Structured Block Plan

> Status: `planning_only`
> Generated at: `2026-06-17T01:45:21Z`
> Upstream gate: D5.3.12 implementation is `codex_review_passed`.
> Boundary: this phase writes only planning artifacts. It does not start implementation.

## 0. Current gate

D5.3.12 current gate:

```text
docs/ai-workgroup/state/artifacts/phase-d5-3-12-adapter-binary-readiness-config-consumer-strict-bool-alignment/acceptance.json
status = codex_review_passed
codex_review.status = passed
codex_review.passed = true
```

D5.3.12 delivered strict literal-bool schema validation for `adapter_binary_readiness` and made malformed readiness config fail closed before any version-probe subprocess. The `adapter-readiness` CLI may return an Orchestrator-only blocked report/event under the reviewed D5.3.12 boundary.

CodeX review included one non-blocking next-slice suggestion: separately check structured handling of `AdapterBinaryReadinessConfigError` in `aiwg/adapter_readiness_gate.py::evaluate_adapter_readiness_gate()`. This plan selects that as the next minimal planning-only slice.

## 1. Safety boundary for D5.3.13 planning-only

Allowed files for this planning-only step:

```text
docs/plans/2026-06-17-aiwg-d5-3-13-adapter-readiness-gate-config-error-structured-block-plan.md
docs/ai-workgroup/state/artifacts/phase-d5-3-13-adapter-readiness-gate-config-error-structured-block-planning/acceptance.json
```

Explicitly forbidden during planning-only:

- D5.3.13 implementation;
- runtime code changes;
- test code changes;
- `aiwg.yaml` changes;
- real agents or external agents;
- real adapter task process execution;
- version-probe enablement;
- MCP mutation tools;
- protected AIVideoTrans business repository writes;
- GitHub write, PR mutation, or PR comments;
- `git commit`, `git push`, or `git merge`;
- deployment;
- CodeX Automation modification.

## 2. Static reconnaissance

Primary target:

```text
aiwg/adapter_readiness_gate.py::evaluate_adapter_readiness_gate
```

Relevant current code points:

```text
aiwg/adapter_readiness_gate.py:10 imports resolve_adapter_binary_readiness only.
aiwg/adapter_readiness_gate.py:163 calls resolve_adapter_binary_readiness(config=..., run_version_probes=False).
aiwg/adapter_readiness_gate.py:163 is not wrapped for AdapterBinaryReadinessConfigError.
```

D5.3.12 introduced:

```text
aiwg/adapter_binary_readiness.py::AdapterBinaryReadinessConfigError
aiwg/adapter_binary_readiness.py::resolve_adapter_binary_readiness
```

Current behavior expectation:

- malformed `adapter_binary_readiness` still fails before a version-probe subprocess because D5.3.12 validates schema at resolver entry;
- but `evaluate_adapter_readiness_gate()` may surface the failure as an exception instead of a standard `AdapterReadinessGateResult(allowed=False, reason=...)`;
- the exception path can prevent `operator_approval.resume_preflight()` or `approve_real_start()` from returning their standard `adapter_readiness_blocked` result and from recording the normal `adapter_readiness_gate_blocked` event.

This is not a D5.3.12 blocker because the subprocess-before-fail boundary was satisfied. It is a cleanup/hardening slice for structured gate behavior.

## 3. Selected D5.3.13 scope

Primary implementation target after review authorization:

```text
aiwg/adapter_readiness_gate.py
```

Primary test target after review authorization:

```text
tests/aiwg/runners/test_d5313_adapter_readiness_gate_config_error_structured_block.py
```

Related consumer assertions may use existing helper patterns from:

```text
tests/aiwg/runners/test_b13_adapter_readiness_gate_binding.py
aiwg/operator_approval.py::resume_preflight
aiwg/operator_approval.py::approve_real_start
```

Expected implementation should remain narrow:

- import `AdapterBinaryReadinessConfigError` in `aiwg/adapter_readiness_gate.py`;
- catch it only around the `resolve_adapter_binary_readiness(..., run_version_probes=False)` current-report refresh;
- return `_blocked("config_contract_invalid", payload, report_path=report_path)` with structured `errors` and `error` fields;
- preserve all existing B13 gate reasons and pass behavior;
- do not change the D5.3.12 resolver/report/doctor implementation unless a test proves a direct bug in that layer.

## 4. Planned behavior contract

### 4.1 Direct gate behavior

When a valid fresh readiness event/report exists but the current runtime config has malformed `adapter_binary_readiness`, `evaluate_adapter_readiness_gate()` should return:

```text
AdapterReadinessGateResult.allowed = false
AdapterReadinessGateResult.reason = "config_contract_invalid"
payload.reason = "config_contract_invalid"
payload.error = "config_contract_invalid"
payload.errors = ["config_contract_invalid: ..." or raw schema errors]
payload.started_real_process = false
payload.started_adapter_process = false
```

It should not raise `AdapterBinaryReadinessConfigError` to the caller.

### 4.2 Resume consumer behavior

For `operator_approval.resume_preflight(...)` in sandbox-plan/sandbox-probe paths, malformed current readiness config should produce the normal gate-blocked result shape:

```text
PreflightResumeResult.status = "adapter_readiness_blocked"
PreflightResumeResult.error = "config_contract_invalid"
```

It should also record an `adapter_readiness_gate_blocked` event whose payload contains:

```text
reason = "config_contract_invalid"
error = "config_contract_invalid"
started_real_process = false
started_adapter_process = false
```

The existing operator approval should remain unused, and no `agent_runs` row should be created.

### 4.3 Real-start consumer behavior

If implementation chooses to cover `approve_real_start(...)` in the same slice, the same gate result should map to:

```text
RealStartAuthorizationResult.status = "adapter_readiness_blocked"
RealStartAuthorizationResult.error = "config_contract_invalid"
```

This remains an authorization-artifact preflight path only. It must not start a real adapter process.

### 4.4 Out-of-scope semantics

D5.3.13 should not widen into a full schema migration for `adapter_readiness_gate` itself. In particular, this slice does not change:

```text
bool(gate_config.get("enabled", True))
_required_modes(...)
_max_age_minutes(...)
bool(adapter_doc.get("available", False))
_validate_codex_lock(...)
```

Those may be future bool-consumer cleanup candidates, but this slice is only about structured handling of D5.3.12's `AdapterBinaryReadinessConfigError` at the gate boundary.

## 5. Planned RED tests for future implementation

These tests must not be added until CodeX passes this planning artifact and Human/CodeX explicitly authorizes implementation.

### Test 1 - direct gate returns structured block for malformed current readiness config

File:

```text
tests/aiwg/runners/test_d5313_adapter_readiness_gate_config_error_structured_block.py
```

Scenario:

1. Use the B13 helper pattern to create a temp project, task, database, manifest, and valid readiness report.
2. After the report exists, mutate current config:

```python
config["adapter_binary_readiness"]["version_probe_enabled"] = "false"
```

3. Call `evaluate_adapter_readiness_gate(..., execution_mode="sandbox_plan")`.

Expected RED result before implementation:

```text
AdapterBinaryReadinessConfigError is raised
```

Expected GREEN result after implementation:

```text
result.allowed is False
result.reason == "config_contract_invalid"
result.payload["reason"] == "config_contract_invalid"
result.payload["error"] == "config_contract_invalid"
result.payload["started_real_process"] is False
result.payload["started_adapter_process"] is False
```

### Test 2 - resume_preflight records standard adapter_readiness_gate_blocked event

Scenario:

1. Create approved preflight with valid config.
2. Write valid readiness report.
3. Mutate current config to malformed `adapter_binary_readiness.version_probe_enabled = "false"`.
4. Call `resume_preflight(...)`.

Expected RED result before implementation:

```text
AdapterBinaryReadinessConfigError escapes the resume call
```

Expected GREEN result after implementation:

```text
result.status == "adapter_readiness_blocked"
result.error == "config_contract_invalid"
agent_runs count remains 0
operator_approvals.used_at remains NULL
latest adapter_readiness_gate_blocked payload.reason == "config_contract_invalid"
latest adapter_readiness_gate_blocked payload.started_real_process is False
latest adapter_readiness_gate_blocked payload.started_adapter_process is False
```

### Test 3 - existing B13 pass/block reasons remain unchanged

Run the existing B13 regression after GREEN:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/aiwg/runners/test_b13_adapter_readiness_gate_binding.py -q -p no:cacheprovider
```

Expected:

```text
all existing B13 tests pass
```

### Optional Test 4 - approve_real_start maps the same gate block

Only include if CodeX wants real-start authorization coverage in D5.3.13.

Expected:

```text
status == "adapter_readiness_blocked"
error == "config_contract_invalid"
no real adapter process starts
```

## 6. GREEN implementation guidance after explicit authorization

Do not implement during planning. If CodeX passes this planning artifact and Human/CodeX authorizes implementation, use strict TDD:

1. Add the D5.3.13 RED test file first.
2. Run the direct-gate and resume tests and confirm the intended exception failure.
3. Import `AdapterBinaryReadinessConfigError` in `aiwg/adapter_readiness_gate.py`.
4. Wrap only the current-report refresh call:

```python
try:
    current_report = resolve_adapter_binary_readiness(
        config=config,
        project_root=project_root,
        run_version_probes=False,
    )
except AdapterBinaryReadinessConfigError as exc:
    return _blocked(
        "config_contract_invalid",
        {
            **payload_with_report,
            "reason": "config_contract_invalid",
            "error": "config_contract_invalid",
            "errors": list(getattr(exc, "errors", [])) or [str(exc)],
        },
        report_path=report_path,
    )
```

5. Run the new D5.3.13 tests.
6. Run B13 regression.
7. Run D5.3.12 targeted regression because this slice consumes D5.3.12's exception:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/aiwg/runners/test_d5312_adapter_binary_readiness_config_consumer_contract_guard.py tests/aiwg/runners/test_b13_adapter_readiness_gate_binding.py -q -p no:cacheprovider
```

8. Run full `tests/aiwg`, `doctor`, MCP `--list-tools`, and protected-repo marker scan.
9. Write implementation acceptance as `completed_ready_for_codex_review`; do not mark CodeX passed until review results are provided.

## 7. Non-goals

Do not include in D5.3.13 implementation unless CodeX explicitly widens scope:

- changing `aiwg.yaml`;
- modifying D5.3.12 resolver/report/doctor behavior;
- requiring checked-in `adapter_binary_readiness` config alignment;
- changing readiness report schema;
- broad `adapter_readiness_gate` bool-consumer cleanup;
- dashboard/status payload normalization;
- real adapter dispatch enablement;
- real adapter task process execution;
- MCP mutation tools;
- protected business repository writes;
- CodeX Automation modification.

## 8. Planning acceptance criteria

This planning-only phase is ready for CodeX quick review when:

1. This plan exists under `docs/plans/`.
2. Planning acceptance exists under `docs/ai-workgroup/state/artifacts/phase-d5-3-13-adapter-readiness-gate-config-error-structured-block-planning/acceptance.json`.
3. D5.3.12 implementation acceptance is `codex_review_passed`.
4. Static reconnaissance records the unhandled gate exception surface.
5. `doctor` remains OK for checked-in `aiwg.yaml`.
6. MCP `--list-tools` remains read-only: `status`, `list_tasks`, `get_task`, `recent_events`.
7. Protected AIVideoTrans marker scan for D5.3.13 terms has `0 hits`.
8. No runtime/test/config/business-repo files are changed by this planning-only step.
9. `codex_review` remains pending until CodeX performs review.

## 9. CodeX quick review focus

Ask CodeX to confirm:

1. D5.3.13 should be the next minimal slice after D5.3.12.
2. The scope should be structured handling of `AdapterBinaryReadinessConfigError` in `evaluate_adapter_readiness_gate()`.
3. `resume_preflight(...)` should receive a standard `adapter_readiness_blocked` result instead of an exception.
4. Whether `approve_real_start(...)` must be covered in this slice or can remain optional follow-up.
5. The slice should not widen into full `adapter_readiness_gate` bool-consumer cleanup.
6. Future implementation must wait for explicit Human/CodeX authorization after planning review passes.

## 10. Recommended next

Submit this D5.3.13 planning-only artifact to CodeX quick review. If CodeX passes, still wait for explicit Human/CodeX implementation authorization before adding RED tests or changing runtime behavior.
