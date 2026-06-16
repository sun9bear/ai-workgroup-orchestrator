# AIWG D5.3.6 Runtime Policy Consumer Strict-Config Alignment Planning

> **For Hermes:** This is a planning-only slice. Do not edit runtime code, tests, `aiwg.yaml`, MCP tools, or the protected business repository while producing this plan. Future implementation must use the `test-driven-development` skill and must keep real agents, writes, MCP mutation tools, GitHub writes, deployment, and CodeX Automation modification disabled unless a later human gate explicitly authorizes a narrower action.

**Goal:** Plan the next minimum hardening slice after D5.3.5 so runtime policy consumers stop relying on `bool(...)` coercion and fail closed on malformed policy values before any real adapter / real agent path can make decisions.

**Architecture:** D5.3.5 made `doctor` and the unified config validator reject unsafe default config values. D5.3.6 should bring the same *literal boolean parsing discipline* to runtime policy entrypoints, without prematurely enabling real execution and without naively reusing the current safe-default validator in a way that would break explicit true-valued test/authorization fixtures. The preferred implementation is a small shared policy-bool schema helper plus a runtime entrypoint guard.

**Tech Stack:** Python 3.11, `pytest`, existing `aiwg.config`, `aiwg.policy`, `aiwg.operator_approval`, and current acceptance artifacts under `docs/ai-workgroup/state/artifacts/`.

---

## Upstream gate

D5.3.5 implementation acceptance is already reviewed:

```text
D:/AIGroup/ai-workgroup-orchestrator/docs/ai-workgroup/state/artifacts/phase-d5-3-5-policy-safety-config-validator/acceptance.json
status = codex_review_passed
codex_review.passed = true
```

Relevant CodeX reminder from D5.3.5:

```text
aiwg/policy.py runtime policy consumers still contain bool(policy.get(...)) style checks;
before any real adapter or real agent phase, align runtime policy entrypoints to the same strict config contract.
```

## Current observed runtime policy consumers

### Primary runtime gate

`aiwg/policy.py`:

```text
policy = config.get("policy") or {}
if bool(policy.get("global_pause", False)):
...
if bool(policy.get("safe_mode", True)):
...
if not bool(policy.get("allow_real_agents", False)):
...
if not bool(policy.get("allow_external_agents", False)):
...
if not bool(policy.get("allow_write", False)):
...
```

Risk: values such as `"false"`, `0`, `[]`, or missing keys can be interpreted through Python truthiness rather than literal policy schema semantics.

### Related direct consumers found during planning reconnaissance

These are real but are **not all selected for the first implementation slice**:

- `aiwg/adapter_registry.py`
  - `policy_snapshot = {key: bool(policy.get(key, False)) ...}`
  - `dispatch_allowed = bool(policy.get("allow_real_adapter_dispatch", False))`
- `aiwg/operator_approval.py`
  - direct checks like `bool((config.get("policy") or {}).get("allow_real_adapter_dispatch", False))`
- `aiwg/git_steward.py`
  - mutation-denial scan uses `bool(policy.get(key, False))`
- `aiwg/d5_preflight.py`
  - `_policy_denials()` still treats truthy values as denials; this is conservative for strings but not a strict schema guard for `0` / missing values.
- `aiwg/doctor.py`
  - still has `_as_bool(...)` messaging checks, but D5.3.5 already runs `validate_config_contract()` first, so malformed defaults fail before those checks can approve the config.

## Selected D5.3.6 surface

Select the smallest high-value runtime surface:

```text
runtime policy entrypoint guard for aiwg.policy.evaluate_runtime_policy()
```

Implementation should focus on:

1. introducing a shared strict literal policy bool accessor/schema helper; and
2. making `evaluate_runtime_policy()` fail closed when policy is malformed, before any `bool(...)` truthiness decision is made.

This addresses the exact CodeX warning (`aiwg/policy.py`) and protects the central claim/dispatch runtime boundary without attempting a full repo-wide policy consumer migration in one slice.

## Important contract nuance

Do **not** simply call current D5.3.5 `validate_config_contract(config)` unconditionally inside every runtime path if that would make all explicit true-valued authorization/test fixtures invalid.

D5.3.5 `validate_config_contract()` is the **safe-default doctor contract**:

```text
safe_mode must be literal True
forbidden action switches must be literal False
```

D5.3.6 runtime alignment should share the same **literal boolean parsing discipline** but keep existing runtime policy semantics:

- policy must be a mapping;
- selected policy keys must be present where required;
- selected values must satisfy `type(value) is bool`;
- once typed, existing runtime logic may still decide whether explicit `True` blocks or allows a path according to the existing policy rules and human authorization gates.

Recommended design:

```python
# aiwg/config.py or a small config-policy helper module
@dataclass(frozen=True)
class PolicyBoolSchemaResult:
    ok: bool
    values: dict[str, bool]
    errors: list[str]


def validate_policy_bool_schema(
    config: dict[str, Any],
    *,
    required_keys: Iterable[str],
) -> PolicyBoolSchemaResult:
    """Fail closed unless policy is a mapping and each required key is literal bool."""
```

Then keep D5.3.5 safe-default validation layered on top:

```python
# validate_config_contract() can call validate_policy_bool_schema(...)
# and then enforce safe_mode=True / forbidden switches=False.
```

Runtime consumers can call the type/schema layer without forcing all values to safe defaults.

## D5.3.6 implementation tasks for the next authorized slice

### Task 1: RED test for malformed policy mapping in runtime gate

**Objective:** Prove `evaluate_runtime_policy()` currently does not fail closed for non-mapping policy values in a controlled, explicit way.

**Files:**

- Create/modify test: `tests/aiwg/test_d536_runtime_policy_contract_guard.py`
- Future runtime code target: `aiwg/policy.py`
- Possible helper target: `aiwg/config.py`

**Test shape:**

```python
def test_d536_runtime_policy_denies_malformed_policy_mapping():
    config = build_default_config()
    config["policy"] = []

    decision = evaluate_runtime_policy(
        config=config,
        project_root=tmp_path,
        agent="Fake",
        adapter_type="fake",
        task={"can_write": False, "requires_human": False},
    )

    assert decision.allowed is False
    assert any("config_contract_invalid" in reason for reason in decision.reasons)
    assert any("policy" in reason for reason in decision.reasons)
```

**Expected RED:** fails because current `config.get("policy") or {}` silently becomes `{}` and can allow fake read-only work.

### Task 2: RED test for bool-coercible policy values

**Objective:** Prove runtime policy decisions reject `"false"` / `0` rather than applying Python truthiness.

**Test examples:**

```python
@pytest.mark.parametrize("key,bad_value", [
    ("safe_mode", "false"),
    ("allow_real_agents", "false"),
    ("allow_external_agents", 0),
    ("allow_write", 0),
])
def test_d536_runtime_policy_rejects_non_literal_policy_bools(tmp_path, key, bad_value):
    config = build_default_config()
    config["policy"][key] = bad_value

    decision = evaluate_runtime_policy(
        config=config,
        project_root=tmp_path,
        agent="Fake",
        adapter_type="fake",
        task={"can_write": False, "requires_human": False},
    )

    assert decision.allowed is False
    assert any(f"policy.{key}" in reason for reason in decision.reasons)
```

**Expected RED:** current code can allow at least some malformed safe-looking values in fake read-only paths.

### Task 3: GREEN helper for literal policy bool schema

**Objective:** Add a reusable helper that checks only policy mapping + literal bool type, not safe-default values.

**Files:**

- Modify: `aiwg/config.py`
- Test: `tests/aiwg/test_d536_runtime_policy_contract_guard.py`

**Implementation guidance:**

- Prefer a helper that returns structured errors rather than raising, so runtime gates can return `RuntimePolicyDecision(allowed=False, reasons=[...])`.
- Do not remove D5.3.5 `validate_config_contract()` semantics.
- Keep `validate_config_contract()` as the safe-default doctor validator.
- Consider reusing `POLICY_FORBIDDEN_FALSE_KEYS` plus runtime-specific keys:

```text
safe_mode
global_pause
allow_real_agents
allow_external_agents
allow_write
```

For `evaluate_runtime_policy()`, this minimum set is enough because those are the keys it consumes directly.

### Task 4: GREEN runtime entrypoint guard

**Objective:** Make `evaluate_runtime_policy()` call the strict type/schema helper before any truthiness logic.

**Files:**

- Modify: `aiwg/policy.py`
- Test: `tests/aiwg/test_d536_runtime_policy_contract_guard.py`

**Required behavior:**

- If policy schema is invalid, return:

```python
RuntimePolicyDecision(
    allowed=False,
    reasons=["config_contract_invalid: policy.<key> ..."]
)
```

- Do not throw raw exceptions for malformed policy.
- Do not enable real adapters or write-capable work.
- Preserve existing denial messages for valid safe-default config.

### Task 5: Downstream guard smoke for real-start authorization path

**Objective:** Prove a malformed policy blocks before the real-start authorization path can interpret `allow_real_adapter_dispatch` via `bool(...)`.

**Files:**

- Test: a focused addition near existing runner/operator approval tests, or in `tests/aiwg/test_d536_runtime_policy_contract_guard.py` with a small fixture.
- Runtime target: ideally no extra code beyond Task 4 if `approve_real_start()` already calls `evaluate_runtime_policy()` first.

**Expectation:**

- malformed `policy.allow_real_adapter_dispatch = "false"` or `0` produces a blocked/policy-denied result containing `config_contract_invalid`;
- no approval is consumed;
- no process is launched;
- no target repo write occurs.

### Task 6: Verification commands

Run from `D:/AIGroup/ai-workgroup-orchestrator`:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg/test_d536_runtime_policy_contract_guard.py -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg/test_d536_runtime_policy_contract_guard.py tests/aiwg/test_d535_policy_safety_config_validator.py tests/aiwg/test_d534_config_validator_doctor.py tests/aiwg/evidence/test_d533_protected_target_roots_contract.py tests/aiwg/test_a0_cli.py -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli doctor --config aiwg.yaml
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.mcp.server --config aiwg.yaml --list-tools
```

Protected business repo marker scan should remain zero-hit for D5.3.6 markers.

## Non-goals for D5.3.6 implementation

Do not include these in the first implementation slice:

- Full repository-wide replacement of every `bool(policy.get(...))` occurrence.
- Enabling real agents, real adapter dispatch, or real process execution.
- Changing MCP tools or adding mutation tools.
- Modifying AIVideoTrans business repository.
- GitHub write API calls, PR comments, PR mutation, push, merge, or deploy.
- CodeX Automation modification.
- Full config schema migration beyond policy bool runtime entrypoint guard.
- Changing D5.3.5 safe-default doctor contract.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Naively calling `validate_config_contract()` in runtime breaks tests/fixtures that intentionally set explicit `True` for simulated authorization paths. | Separate literal bool schema validation from safe-default value validation. Runtime uses type/schema helper; doctor keeps safe-default contract. |
| Only guarding `evaluate_runtime_policy()` misses direct policy bool consumers elsewhere. | Treat D5.3.6 as first runtime entrypoint slice. Use acceptance artifact to record remaining direct consumers for later D5.3.7+ planning. |
| Malformed config causes raw exceptions in runtime paths. | Runtime guard returns structured denial reasons and tests assert no raw crash. |
| Accidental capability enablement while aligning config. | No `aiwg.yaml` changes in planning; future implementation must not set safety switches true. |

## Planning acceptance criteria

This planning slice is complete when:

- D5.3.5 upstream acceptance is confirmed `codex_review_passed`.
- This plan is saved under `docs/plans/`.
- A planning acceptance artifact is written with `status=completed_ready_for_codex_review` and `codex_review.status=pending`.
- Runtime code, tests, `aiwg.yaml`, MCP tools, and protected business repo are unchanged in this planning slice.
- Safe read-only checks are recorded:
  - `doctor: AIWG doctor OK`
  - MCP surface remains `status`, `list_tasks`, `get_task`, `recent_events`
  - AIVideoTrans D5.3.6 marker scan has `0 hits`

## Recommended next after CodeX review

If CodeX approves this planning artifact, proceed to:

```text
D5.3.6 implementation - strict literal policy bool schema helper + evaluate_runtime_policy() fail-closed entrypoint guard, using strict TDD.
```

Keep fake/dry-run/preflight-only. Do not mark implementation `codex_review_passed` until CodeX independently reviews and passes it.
