# AIWG D5.3.14 Adapter Readiness Gate Enabled Contract Plan

> Status: `planning_only`
> Generated at: `2026-06-17T02:47:55Z`
> Upstream gate: D5.3.13 implementation is `codex_review_passed`.
> Boundary: this phase writes only planning artifacts. It does not start implementation.

## 0. Current gate

D5.3.13 implementation acceptance is closed:

```text
docs/ai-workgroup/state/artifacts/phase-d5-3-13-adapter-readiness-gate-config-error-structured-block/acceptance.json
status = codex_review_passed
codex_review.status = passed
codex_review.passed = true
```

D5.3.13 intentionally did not widen into broader `adapter_readiness_gate` bool-consumer cleanup. Its plan recorded these future candidates:

```text
bool(gate_config.get("enabled", True))
_required_modes(...)
_max_age_minutes(...)
bool(adapter_doc.get("available", False))
_validate_codex_lock(...)
```

D5.3.14 selects the smallest high-value item from that list: `adapter_readiness_gate.enabled`.

## 1. Why this is the next minimal slice

Current code in `aiwg/adapter_readiness_gate.py` uses truthiness to decide whether the gate is disabled:

```python
gate_config = _gate_config(config)
if not bool(gate_config.get("enabled", True)):
    return AdapterReadinessGateResult(
        allowed=True,
        payload={"gate_enabled": False, "execution_mode": execution_mode},
    )
```

This creates a fail-open config-contract gap:

- absent `adapter_readiness_gate` should remain safe and default to enabled;
- literal `enabled: false` may still intentionally skip the gate in existing tests/fixtures;
- but malformed falsey values such as `0`, `None`, `[]`, `{}` can currently disable the gate through `bool(...)` coercion;
- that skip happens before readiness-report checks, current binary checks, Codex automation lock checks, `resume_preflight(...)` blocking, and `approve_real_start(...)` blocking.

This is more urgent than report-shape bool cleanup because it is a config-consumer branch that can bypass the whole B13/D5.3.13 gate.

## 2. Safety boundary for D5.3.14 planning-only

Allowed files for this planning-only step:

```text
docs/plans/2026-06-17-aiwg-d5-3-14-adapter-readiness-gate-enabled-contract-plan.md
docs/ai-workgroup/state/artifacts/phase-d5-3-14-adapter-readiness-gate-enabled-contract-planning/acceptance.json
```

Explicitly forbidden during planning-only:

- D5.3.14 implementation;
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

## 3. Static reconnaissance

Selected current code points:

```text
aiwg/adapter_readiness_gate.py:43 gate_config = _gate_config(config)
aiwg/adapter_readiness_gate.py:44 if not bool(gate_config.get("enabled", True)):
aiwg/adapter_readiness_gate.py:268 _gate_config(config) returns {} for absent or non-mapping section
```

Related contract infrastructure:

```text
aiwg/config.py:264 validate_adapter_binary_readiness_bool_schema(...)
aiwg/config.py:191 validate_config_contract(...)
aiwg/doctor.py consumes validate_config_contract(...)
```

Existing regression anchor:

```text
tests/aiwg/runners/test_b13_adapter_readiness_gate_binding.py
tests/aiwg/runners/test_d5313_adapter_readiness_gate_config_error_structured_block.py
```

Checked-in `aiwg.yaml` currently has no `adapter_readiness_gate` section. Future implementation must preserve this as absent-compatible and safe: default enabled behavior, no doctor failure.

Static bool scan still reports these `adapter_readiness_gate.py` bool consumers:

```text
line 44:  bool(gate_config.get("enabled", True))
line 150: bool(adapter_doc.get("available", False))
line 356: bool(report_codex.get("desktop_automation_allowed", False))
line 357: bool(manifest_codex.get("desktop_automation_allowed", False))
```

D5.3.14 only scopes line 44. The report/manifest bool consumers should remain future report-schema hardening candidates unless CodeX explicitly widens this plan.

## 4. Selected D5.3.14 scope

Primary implementation target after CodeX/Human authorization:

```text
aiwg/config.py
aiwg/adapter_readiness_gate.py
aiwg/doctor.py
```

Primary test target after CodeX/Human authorization:

```text
tests/aiwg/runners/test_d5314_adapter_readiness_gate_enabled_contract_guard.py
```

Expected helper shape:

```python
ADAPTER_READINESS_GATE_BOOL_DEFAULTS = {
    "enabled": True,
}

def validate_adapter_readiness_gate_bool_schema(config: Config) -> PolicyBoolSchemaResult:
    ...
```

Implementation should be absent-compatible but strict when the section/key is present:

- if `adapter_readiness_gate` is absent: return `ok=True`, `enabled=True`;
- if `adapter_readiness_gate` is not a mapping: return `config_contract_invalid`;
- if `adapter_readiness_gate.enabled` is absent: return `enabled=True`;
- if `adapter_readiness_gate.enabled` is present, it must be literal `bool`;
- `0`, `1`, `"false"`, `"true"`, `None`, list, and object must be invalid, not coerced.

## 5. Planned behavior contract

### 5.1 Direct gate behavior

For malformed falsey gate config:

```python
config["adapter_readiness_gate"]["enabled"] = 0
```

`evaluate_adapter_readiness_gate(...)` should fail closed:

```text
AdapterReadinessGateResult.allowed = false
AdapterReadinessGateResult.reason = "config_contract_invalid"
payload.reason = "config_contract_invalid"
payload.error = "config_contract_invalid"
payload.errors contains "adapter_readiness_gate.enabled must be literal bool"
payload.started_real_process = false
payload.started_adapter_process = false
```

It must not return `allowed=True` with `gate_enabled=False`.

### 5.2 Resume consumer behavior

For `operator_approval.resume_preflight(...)`, malformed `adapter_readiness_gate.enabled = 0` should return:

```text
PreflightResumeResult.status = "adapter_readiness_blocked"
PreflightResumeResult.error = "config_contract_invalid"
```

It should record an `adapter_readiness_gate_blocked` event with:

```text
reason = "config_contract_invalid"
error = "config_contract_invalid"
started_real_process = false
started_adapter_process = false
```

No sandbox invocation plan should be produced through a malformed gate-disabled branch.

### 5.3 Real-start consumer behavior

For `operator_approval.approve_real_start(...)`, malformed `adapter_readiness_gate.enabled = 0` should return:

```text
RealStartAuthorizationResult.status = "adapter_readiness_blocked"
RealStartAuthorizationResult.error = "config_contract_invalid"
```

It must not produce a real-start authorization artifact and must not start or authorize a real adapter process.

### 5.4 Doctor/config behavior

`validate_config_contract(...)` and `doctor` should include the new schema:

```text
adapter_readiness_gate bool schema ok
```

Malformed present values should make doctor fail closed with non-zero exit and a `config_contract_invalid` error. Checked-in `aiwg.yaml` should remain valid because its missing `adapter_readiness_gate` section uses the absent-compatible default.

### 5.5 Preserved behavior

Literal bools preserve current behavior:

- `enabled: true` continues normal gate checks;
- `enabled: false` continues the existing explicit skip semantics;
- missing section or missing key defaults to enabled.

## 6. Planned RED tests for future implementation

These tests must not be added until CodeX passes this planning artifact and Human/CodeX authorizes implementation.

### Test 1 - direct gate does not let malformed falsey enabled disable the gate

File:

```text
tests/aiwg/runners/test_d5314_adapter_readiness_gate_enabled_contract_guard.py
```

Scenario:

1. Create a valid temp project, DB, task, manifest, and readiness report using the B13 helper style.
2. Mutate config after setup:

```python
config["adapter_readiness_gate"]["enabled"] = 0
```

Expected RED result before implementation:

```text
result.allowed is True
result.payload["gate_enabled"] is False
```

Expected GREEN result after implementation:

```text
result.allowed is False
result.reason == "config_contract_invalid"
result.payload["error"] == "config_contract_invalid"
```

### Test 2 - resume_preflight records standard blocked event

Scenario:

1. Create approved preflight.
2. Write valid readiness report.
3. Set `adapter_readiness_gate.enabled = 0`.
4. Call `resume_preflight(...)`.

Expected RED result before implementation:

```text
status is not "adapter_readiness_blocked"; the gate is skipped as disabled
```

Expected GREEN result after implementation:

```text
result.status == "adapter_readiness_blocked"
result.error == "config_contract_invalid"
latest adapter_readiness_gate_blocked payload.reason == "config_contract_invalid"
latest payload.started_real_process is False
latest payload.started_adapter_process is False
```

### Test 3 - approve_real_start is covered too

Use the B19/B20 or D5.3.13 chain pattern to create a plan/probe chain, then mutate:

```python
config["adapter_readiness_gate"]["enabled"] = 0
```

Expected GREEN result:

```text
result.status == "adapter_readiness_blocked"
result.error == "config_contract_invalid"
result.authorization_path is None
```

### Test 4 - literal false remains an explicit skip

Set:

```python
config["adapter_readiness_gate"]["enabled"] = False
```

Expected:

```text
evaluate_adapter_readiness_gate(...).allowed is True
payload.gate_enabled is False
```

This protects backward compatibility for explicit, literal bool gate disablement.

### Test 5 - doctor rejects malformed present enabled and accepts absent section

Add CLI/subprocess coverage similar to D5.3.12/D5.3.13 validator tests:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli doctor --config malformed-gate.yaml
```

Expected malformed result:

```text
exit_code != 0
config_contract_invalid
adapter_readiness_gate.enabled must be literal bool
```

Expected checked-in `aiwg.yaml` result:

```text
AIWG doctor: OK
adapter_readiness_gate bool schema ok
```

## 7. GREEN implementation guidance after explicit authorization

Do not implement during planning. If CodeX passes this planning artifact and implementation is authorized, use strict TDD:

1. Add `tests/aiwg/runners/test_d5314_adapter_readiness_gate_enabled_contract_guard.py` first.
2. Run it and confirm the intended RED failures: malformed `enabled=0` skips the gate instead of blocking.
3. Add `ADAPTER_READINESS_GATE_BOOL_DEFAULTS` and `validate_adapter_readiness_gate_bool_schema(...)` in `aiwg/config.py`.
4. Wire the validator into `validate_config_contract(...)` and `doctor` messages.
5. In `evaluate_adapter_readiness_gate(...)`, check the schema before the `if not enabled` branch. On schema error, return `_blocked("config_contract_invalid", ...)` with no process side effects.
6. Replace `bool(gate_config.get("enabled", True))` with the strict schema value.
7. Run D5.3.14 targeted tests.
8. Run B13, D5.3.13, B19/B20 targeted regression.
9. Run full `tests/aiwg`, `doctor`, MCP `--list-tools`, and protected-repo marker scan.
10. Write implementation acceptance as `completed_ready_for_codex_review`; do not mark CodeX passed until review results are provided.

## 8. Non-goals

Do not include in D5.3.14 unless CodeX explicitly widens scope:

- changing `aiwg.yaml`;
- changing `adapter_binary_readiness` schema or D5.3.12 behavior;
- changing D5.3.13 `AdapterBinaryReadinessConfigError` structured block behavior;
- changing `_required_modes(...)` or `_max_age_minutes(...)`;
- validating readiness report schema fields such as `adapter_doc.available`;
- validating Codex report/manifest `desktop_automation_allowed` fields;
- dashboard/status payload normalization;
- real adapter dispatch enablement;
- real adapter task process execution;
- MCP mutation tools;
- protected business repository writes;
- CodeX Automation modification.

## 9. Planning acceptance criteria

This planning-only phase is ready for CodeX quick review when:

1. This plan exists under `docs/plans/`.
2. Planning acceptance exists under `docs/ai-workgroup/state/artifacts/phase-d5-3-14-adapter-readiness-gate-enabled-contract-planning/acceptance.json`.
3. D5.3.13 implementation acceptance is `codex_review_passed`.
4. Static reconnaissance records the current `enabled` truthiness skip surface.
5. `doctor` remains OK for checked-in `aiwg.yaml`.
6. MCP `--list-tools` remains read-only: `status`, `list_tasks`, `get_task`, `recent_events`.
7. Protected AIVideoTrans marker scan for D5.3.14 terms has `0 hits`.
8. No runtime/test/config/business-repo files are changed by this planning-only step.
9. `codex_review` remains pending until CodeX performs review.

## 10. CodeX quick review focus

Ask CodeX to confirm:

1. D5.3.14 should target `adapter_readiness_gate.enabled` as the next minimal high-value bool-consumer slice.
2. Malformed falsey values like `0` must block with `config_contract_invalid`, not disable the gate.
3. Literal `False` may preserve the explicit skip behavior.
4. Absent section/key must remain safe and default enabled.
5. `resume_preflight(...)` and `approve_real_start(...)` should both be covered in implementation tests.
6. The slice should not widen into report-schema cleanup for `adapter_doc.available` or Codex desktop automation bools.
7. Future implementation must wait for explicit Human/CodeX authorization after planning review passes.

## 11. Recommended next

Submit this D5.3.14 planning-only artifact to CodeX quick review. If CodeX passes, still wait for explicit Human/CodeX implementation authorization before adding RED tests or changing runtime behavior.
