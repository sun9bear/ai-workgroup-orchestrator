# AIWG D5.3.5 Policy Safety Config Validator Planning

> **For Hermes:** Use test-driven-development skill for the future GREEN implementation slice. Do not enable real agents, writes, MCP mutation tools, GitHub mutation, deployment, or CodeX Automation changes.

**Goal:** Choose the next smallest config schema surface to add to `validate_config_contract()` / `doctor` after D5.3.4.

**Architecture:** Extend the D5.3.4 unified config validator one narrow safety surface at a time. D5.3.5 is planning-only: it selects the surface, writes TDD implementation tasks, and records safety evidence without changing runtime behavior.

**Tech Stack:** Python 3.11, pytest, `aiwg.config.validate_config_contract`, `aiwg.doctor.run_doctor`, `aiwg.d5_preflight.FORBIDDEN_POLICY_KEYS`, YAML config (`aiwg.yaml`).

---

## 0. Current gate

D5.3.4 is already `codex_review_passed`:

```text
docs/ai-workgroup/state/artifacts/phase-d5-3-4-config-validator-doctor/acceptance.json
```

D5.3.4 introduced:

- `aiwg.config.ConfigValidationResult`
- `aiwg.config.validate_config_contract(config)`
- `doctor` integration for `protected_target_roots`

Current validator scope is intentionally narrow: only `protected_target_roots`.

## 1. Safety boundary

D5.3.5 planning remains fake/dry-run/preflight-only.

Strictly forbidden:

- real agents / external agents;
- MCP mutation tools;
- protected AIVideoTrans business repository writes;
- GitHub write API / PR mutation / PR comment;
- `git push`, `git merge`, deployment;
- CodeX Automation modification;
- broad config-schema migration in one step;
- large shared-helper refactor.

Allowed D5.3.5 planning-only writes are limited to Orchestrator-owned evidence/docs:

```text
docs/plans/2026-06-08-aiwg-d5-3-5-policy-safety-config-validator-plan.md
docs/ai-workgroup/state/artifacts/phase-d5-3-5-policy-safety-config-validator-planning/acceptance.json
```

No runtime/tests/business-repo changes are required for planning.

## 2. Candidate surfaces considered

| Candidate | Value | Risk / size | Decision |
| --- | --- | --- | --- |
| `protected_target_roots` | Already done in D5.3.4 | Complete | Do not repeat |
| Full config schema | Comprehensive | Too broad; likely causes unrelated breakage | Defer |
| `artifact_root` / `state_db` root schema | High path-safety value | Already covered by D5.3.1-D5.3.3 runtime guards; schema changes can couple to many callers | Defer to a dedicated path-root slice |
| `adapter_binary_readiness` schema | Small | Less central than policy gates; optional in live config | Defer |
| `policy` dangerous action switches | High safety value; directly aligned with D5 `FORBIDDEN_POLICY_KEYS`; already checked by `doctor`/D5 preflight but with permissive `bool(...)` coercion | Narrow enough if limited to forbidden switches only | **Select for D5.3.5 implementation** |

## 3. Selected D5.3.5 surface

D5.3.5 should add a config validator contract for the **policy forbidden action switches**.

Selected keys are the D5 preflight forbidden policy keys:

```python
FORBIDDEN_POLICY_KEYS = (
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
```

Plus `safe_mode`, because it is the top-level safety posture switch already checked by `doctor`.

### Target contract

- `policy` must be a mapping.
- `policy.safe_mode` must be present and literal `True`.
- Each forbidden action switch must be present and literal `False` under `policy`.
- Malformed values must fail closed, including:
  - missing key;
  - `None`;
  - string values such as `"false"`, `"true"`, `"0"`;
  - integers/floats such as `0`, `1`;
  - lists/tuples/dicts/objects.
- The validator should report errors with paths like:
  - `policy.safe_mode`
  - `policy.allow_write`
  - `policy.allow_real_process_execution`
- The implementation must not enable any currently forbidden action.

### Live config alignment note

Current `aiwg.yaml` contains most but not all newer D5 default policy keys. The D5.3.5 implementation slice may need a small config-only alignment to add missing policy keys with safe values, for example:

```yaml
policy:
  allow_real_adapter_dispatch: false
  allow_real_process_execution: false
```

This is not an enablement: both values remain `false`.

## 4. Non-goals for D5.3.5 implementation

Do not include:

- full `policy` schema validation;
- timeout numeric schema (`default_timeout_minutes`, `default_max_attempts`);
- `global_kill_switch` path validation;
- `git` schema validation;
- `agents` schema validation;
- adapter readiness schema validation;
- `d5_preflight` budget/lease schema validation;
- runtime behavior changes outside `validate_config_contract()` and `doctor` reporting;
- D5 preflight `_policy_denials()` refactor unless a RED test proves validator integration requires it.

## 5. Future implementation plan

### Task 1: Add RED tests for valid default policy safety schema

**Objective:** Prove the unified validator accepts the safe default config and reports policy safety schema success.

**Files:**

- Test: `tests/aiwg/test_d535_policy_safety_config_validator.py`
- Read-only reference: `aiwg/config.py`

**Step 1: Write failing test**

Add a test that calls:

```python
from aiwg.config import build_default_config, validate_config_contract


def test_d535_validator_accepts_default_policy_safety_contract() -> None:
    result = validate_config_contract(build_default_config())

    assert result.ok is True
    assert any("policy safety schema ok" in message for message in result.messages)
```

**Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg/test_d535_policy_safety_config_validator.py -p no:cacheprovider
```

Expected before GREEN: fail because no policy-safety message/contract exists.

### Task 2: Add RED tests for malformed `policy` top-level shape

**Objective:** `policy` must be a mapping; malformed top-level `policy` must fail closed.

**Files:**

- Test: `tests/aiwg/test_d535_policy_safety_config_validator.py`

**Step 1: Write failing tests**

Parametrize values:

```python
None, [], "", "policy", 0, True, object()
```

Expected:

```python
assert result.ok is False
assert any("policy" in error for error in result.errors)
```

### Task 3: Add RED tests for `safe_mode` literal `True`

**Objective:** Avoid permissive bool coercion for the most important safety posture key.

**Files:**

- Test: `tests/aiwg/test_d535_policy_safety_config_validator.py`

**Step 1: Write failing tests**

Reject values:

```python
False, None, "true", "false", "1", 1, [], {}, object()
```

Expected error path includes:

```text
policy.safe_mode
```

### Task 4: Add RED tests for forbidden action switches literal `False`

**Objective:** Prevent strings, numbers, missing values, or arbitrary shapes from passing as disabled safety switches.

**Files:**

- Test: `tests/aiwg/test_d535_policy_safety_config_validator.py`

**Step 1: Write failing tests**

For each selected key:

```python
allow_write
allow_real_agents
allow_external_agents
allow_real_adapter_dispatch
allow_real_process_execution
allow_push
allow_merge
allow_deploy
allow_secret_access
allow_modify_codex_automations
allow_destructive_commands
```

Reject values:

```python
True, None, "false", "0", 0, [], {}, object()
```

Also test a missing-key copy of the config fails closed.

### Task 5: Implement minimal GREEN in `validate_config_contract()`

**Objective:** Add policy-safety schema checks only, preserving existing `protected_target_roots` validation.

**Files:**

- Modify: `aiwg/config.py`

**Implementation direction:**

Add local helper functions in `config.py`:

```python
def _validate_policy_safety_contract(config: Config) -> ConfigValidationResult:
    ...


def _expect_literal(policy: dict[str, Any], key: str, expected: bool, errors: list[str]) -> None:
    ...
```

Rules:

- use `type(value) is bool`, not `bool(value)`;
- missing key is an error;
- expected value mismatch is an error;
- append one success message when all checks pass:

```text
policy safety schema ok: forbidden action switches disabled
```

### Task 6: Align live `aiwg.yaml` only if required

**Objective:** Keep live doctor green without enabling anything.

**Files:**

- Modify only if tests/doctor require it: `aiwg.yaml`

Add missing D5 safety keys with false values. Do not set any switch true.

### Task 7: Verify doctor fail-closed behavior

**Objective:** `doctor` should surface validator errors before broader execution surfaces.

**Files:**

- Test: `tests/aiwg/test_d535_policy_safety_config_validator.py`

Add CLI coverage similar to D5.3.4:

```bash
python -m aiwg.cli doctor --config <invalid-config> --project-root <tmp>
```

Expected:

```text
exit_code = 1
AIWG doctor: FAILED
[ERROR] policy.allow_write ...
```

### Task 8: Run verification

Expected command set:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg/test_d535_policy_safety_config_validator.py -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg/test_d534_config_validator_doctor.py tests/aiwg/evidence/test_d533_protected_target_roots_contract.py tests/aiwg/test_a0_cli.py -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli doctor --config aiwg.yaml
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.mcp.server --config aiwg.yaml --list-tools
```

Expected safety surface:

```text
AIWG doctor: OK
MCP tools: status/list_tasks/get_task/recent_events only
```

### Task 9: Write D5.3.5 implementation acceptance

**Objective:** Record implementation evidence only after GREEN.

**Files:**

- Create: `docs/ai-workgroup/state/artifacts/phase-d5-3-5-policy-safety-config-validator/acceptance.json`

Initial status should be:

```text
completed_ready_for_codex_review
```

Do not mark `codex_review_passed` until CodeX explicitly reviews and passes.

## 6. Planning acceptance criteria

D5.3.5 planning is complete when:

- this plan exists under `docs/plans/`;
- an acceptance artifact exists under Orchestrator state artifacts;
- no runtime code is changed in the planning slice;
- no tests are changed in the planning slice;
- AIVideoTrans has no D5.3.5 marker writes;
- `doctor` remains OK;
- MCP surface remains read-only;
- next implementation slice is narrow enough to be TDD-driven and CodeX-reviewable.

## 7. Recommended next

Submit this D5.3.5 planning artifact for CodeX review. After review passes, implement the selected policy safety config validator slice with strict TDD.
