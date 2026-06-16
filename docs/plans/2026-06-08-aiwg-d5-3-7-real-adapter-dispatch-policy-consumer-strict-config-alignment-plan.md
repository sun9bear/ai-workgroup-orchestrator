# AIWG D5.3.7 Real Adapter Dispatch Policy Consumer Strict-Config Alignment Planning

> **For Hermes:** This is a planning-only slice. Do not edit runtime code, tests, `aiwg.yaml`, MCP tools, GitHub state, CodeX Automations, or the protected AIVideoTrans business repository while producing this plan. Future implementation must use strict TDD: RED tests first, then the smallest GREEN change, then targeted and safety regressions.

**Goal:** Plan the next minimum runtime policy-consumer hardening slice after D5.3.6: make the `allow_real_adapter_dispatch` consumers in `adapter_registry` and `operator_approval` fail closed on malformed/non-literal policy values instead of relying on Python `bool(...)` coercion.

**Architecture:** D5.3.6 introduced `aiwg.config.validate_policy_bool_schema()` and wired it into `aiwg.policy.evaluate_runtime_policy()` without conflating runtime type validation with D5.3.5 safe-default validation. D5.3.7 should reuse that literal-bool schema helper for the real-adapter dispatch boundary only, preserving existing literal `False`/`True` semantics and keeping all real execution gates disabled by default. This is a narrow consumer-alignment slice, not a repo-wide policy migration.

**Tech Stack:** Python 3.11, `pytest`, `aiwg.config.validate_policy_bool_schema`, `aiwg.adapter_registry`, `aiwg.operator_approval`, existing B6/B20 real-adapter lifecycle tests, acceptance artifacts under `docs/ai-workgroup/state/artifacts/`.

---

## Upstream gate

D5.3.6 implementation has passed CodeX review:

```text
D:/AIGroup/ai-workgroup-orchestrator/docs/ai-workgroup/state/artifacts/phase-d5-3-6-runtime-policy-consumer-strict-config-alignment/acceptance.json
status = codex_review_passed
codex_review.status = passed
codex_review.passed = true
```

Relevant D5.3.6 deferred consumers:

```text
aiwg/adapter_registry.py policy_snapshot and dispatch_allowed bool coercion
aiwg/operator_approval.py direct allow_real_adapter_dispatch bool checks
```

## Current code reconnaissance

### Existing strict helper

`aiwg/config.py:225` already provides the reusable type/schema layer:

```python
def validate_policy_bool_schema(config: Config, *, required_keys: Iterable[str]) -> PolicyBoolSchemaResult:
    """Validate runtime-consumed policy booleans without enforcing safe-default values."""
```

Contract to preserve:

- `policy` must be a mapping;
- every selected key must be present;
- every selected value must satisfy `type(value) is bool`;
- the helper does **not** require safe defaults such as `safe_mode=True` or `allow_* = False`.

### Adapter manifest consumer

`aiwg/adapter_registry.py:109-129` currently does:

```python
policy = config.get("policy") or {}
policy_snapshot = {
    key: bool(policy.get(key, False))
    for key in policy_keys
}
dispatch_allowed = bool(policy.get("allow_real_adapter_dispatch", False))
```

Risk examples:

- `policy["allow_real_adapter_dispatch"] = "false"` becomes `True` and can remove the `start_real_agent_process` forbidden side-effect from the manifest.
- `policy = []` silently becomes `{}` and hides a malformed config as if it were a normal closed policy.
- `0` / `1` are accepted as booleans by implication rather than schema.

### Operator approval / resume consumer

`aiwg/operator_approval.py:274` and `aiwg/operator_approval.py:749` currently do:

```python
if not bool((config.get("policy") or {}).get("allow_real_adapter_dispatch", False)):
    ...
```

Risk examples:

- `"false"` is truthy and can bypass the intended dispatch block.
- `0` is falsy but accepted silently instead of being reported as `config_contract_invalid`.
- non-mapping `policy` can be masked by `or {}`.

`operator_approval` already calls `evaluate_runtime_policy()` before the direct dispatch checks, but D5.3.6 intentionally selected only these runtime keys:

```text
global_pause, safe_mode, allow_real_agents, allow_external_agents, allow_write
```

Therefore `allow_real_adapter_dispatch` still needs its own direct consumer guard in D5.3.7.

## Selected D5.3.7 implementation surface

Include only:

1. `aiwg/adapter_registry.py`
   - manifest `policy_snapshot` construction;
   - manifest `dispatch_allowed` calculation;
   - manifest safety metadata when dispatch policy schema is invalid.
2. `aiwg/operator_approval.py`
   - `approve_real_start(...)` direct `allow_real_adapter_dispatch` check;
   - `resume_preflight(...)` direct `allow_real_adapter_dispatch` check.
3. Focused tests, preferably new file:
   - `tests/aiwg/test_d537_dispatch_policy_consumer_contract_guard.py`
   - plus existing B6/B20 regression files when necessary.

Explicitly exclude from this slice:

- `aiwg/git_steward.py` mutation policy denials;
- `aiwg/d5_preflight.py` `_policy_denials()`;
- `aiwg/doctor.py` messaging-only `_as_bool` paths;
- any MCP mutation tools;
- any `aiwg.yaml` safety switch changes;
- any AIVideoTrans business-repository writes;
- any real agent process start, GitHub write API, push, merge, deploy, or CodeX Automation modification.

## Intended behavior contract

### Literal `False`

Preserve existing closed behavior:

```text
allow_real_adapter_dispatch = False
```

- `adapter_registry` manifest has `dispatch_allowed = false`.
- `start_real_agent_process` remains in `forbidden_side_effects`.
- `approve_real_start(...)` returns the existing blocked result with `error="allow_real_adapter_dispatch=false"`.
- `resume_preflight(...)` returns the existing real-dispatch block with `error="allow_real_adapter_dispatch=false"`.

### Literal `True`

Preserve existing explicit-authorization fixture behavior:

```text
allow_real_adapter_dispatch = True
```

- Do not reject solely because the value is `True`.
- Continue to require the other gates: runtime policy, scope gate, readiness gate, preflight approval, sandbox plan/probe, real-start authorization, process-execution policy, and execution-mode checks.
- This slice must not make real execution possible by itself.

### Malformed/non-literal values

Fail closed before Python truthiness decisions:

```text
policy = []
policy.allow_real_adapter_dispatch = "false"
policy.allow_real_adapter_dispatch = 0
policy.allow_real_adapter_dispatch = 1
missing policy.allow_real_adapter_dispatch
```

Expected shape:

```text
config_contract_invalid: policy.allow_real_adapter_dispatch must be literal bool; got str/int/...
```

Consumer-specific outputs:

- `adapter_registry` manifest should set `dispatch_allowed = false`, keep `start_real_agent_process` forbidden, and include machine-readable schema failure metadata.
- `operator_approval` should return `status="policy_denied"` with `policy_reasons` containing `config_contract_invalid: ...` for malformed schema.
- Existing literal `False` should keep existing `blocked` / `real_dispatch_blocked` results rather than becoming a schema error.

## Recommended implementation design

### Helper shape

Use the existing generic helper rather than introducing a new boolean coercion path:

```python
# aiwg/operator_approval.py or aiwg/adapter_registry.py local constant
REAL_ADAPTER_DISPATCH_POLICY_BOOL_KEYS = ("allow_real_adapter_dispatch",)

schema = validate_policy_bool_schema(
    config,
    required_keys=REAL_ADAPTER_DISPATCH_POLICY_BOOL_KEYS,
)
```

For `adapter_registry`, the manifest has a broader `policy_snapshot`. Recommended key set:

```python
ADAPTER_MANIFEST_POLICY_SNAPSHOT_KEYS = (
    "safe_mode",
    "allow_real_agents",
    "allow_external_agents",
    "allow_real_adapter_dispatch",
    "allow_write",
    "allow_push",
    "allow_merge",
    "allow_deploy",
    "allow_destructive_commands",
    "allow_network_write",
    "allow_secret_access",
    "allow_modify_codex_automations",
)
```

Recommended adapter manifest behavior:

```python
schema = validate_policy_bool_schema(config, required_keys=ADAPTER_MANIFEST_POLICY_SNAPSHOT_KEYS)
policy_snapshot = {key: schema.values.get(key, False) for key in ADAPTER_MANIFEST_POLICY_SNAPSHOT_KEYS}
dispatch_allowed = schema.ok and schema.values.get("allow_real_adapter_dispatch") is True
config_contract_errors = [f"config_contract_invalid: {error}" for error in schema.errors]
```

Add manifest metadata only; do not start a process:

```python
"config_contract_valid": schema.ok,
"config_contract_errors": config_contract_errors,
```

Recommended operator approval helper:

```python
def _real_adapter_dispatch_policy_reasons(config: dict[str, Any]) -> list[str]:
    schema = validate_policy_bool_schema(
        config,
        required_keys=("allow_real_adapter_dispatch",),
    )
    if not schema.ok:
        return [f"config_contract_invalid: {error}" for error in schema.errors]
    if not schema.values["allow_real_adapter_dispatch"]:
        return ["allow_real_adapter_dispatch=false"]
    return []
```

When reasons start with `config_contract_invalid`, return `status="policy_denied"` and populate `policy_reasons`; when the only reason is literal false, preserve the current `blocked` / `real_dispatch_blocked` status and error text.

## TDD implementation tasks for the next authorized slice

### Task 1: RED adapter manifest rejects truthy string dispatch policy

**Objective:** Prove `adapter_registry` currently treats `"false"` as dispatch authorization.

**Files:**

- Create: `tests/aiwg/test_d537_dispatch_policy_consumer_contract_guard.py`
- Future modify: `aiwg/adapter_registry.py`

**Test sketch:**

```python
from pathlib import Path

from aiwg.adapter_registry import build_restricted_adapter_manifest
from aiwg.config import build_default_config


def _manifest_task() -> dict[str, object]:
    return {
        "id": "D537-msg-dispatch-policy",
        "task_id": "D537-dispatch-policy",
        "message_path": "docs/ai-workgroup/inbox/OpenCode/msg.md",
        "from_agent": "CodeX",
        "to_agent": "OpenCode",
        "type": "instruction",
        "can_write": False,
        "requires_human": False,
        "allowed_files": [],
        "forbidden_files": [],
        "context_files": [],
        "acceptance": [],
        "attempt": 0,
        "max_attempts": 2,
        "timeout_minutes": 30,
    }


def test_d537_manifest_rejects_non_literal_dispatch_policy_string(tmp_path: Path) -> None:
    config = build_default_config(project_root=tmp_path)
    config["policy"].update({
        "safe_mode": False,
        "allow_real_agents": True,
        "allow_external_agents": True,
        "allow_real_adapter_dispatch": "false",
    })

    manifest = build_restricted_adapter_manifest(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        adapter_type="opencode",
        task=_manifest_task(),
        manifest_path=tmp_path / "adapter-preflight.json",
        prompt_path=tmp_path / "adapter-prompt.md",
    )

    assert manifest["dispatch_allowed"] is False
    assert manifest["config_contract_valid"] is False
    assert any("config_contract_invalid" in error for error in manifest["config_contract_errors"])
    assert any("policy.allow_real_adapter_dispatch" in error for error in manifest["config_contract_errors"])
    assert "start_real_agent_process" in manifest["forbidden_side_effects"]
```

**Expected RED:** current code sets `dispatch_allowed` to `True` for `"false"` and lacks `config_contract_valid` / `config_contract_errors` metadata.

### Task 2: GREEN adapter manifest schema guard

**Objective:** Replace `bool(policy.get(...))` in `build_restricted_adapter_manifest()` with `validate_policy_bool_schema()` for manifest snapshot keys.

**Files:**

- Modify: `aiwg/adapter_registry.py`
- Test: `tests/aiwg/test_d537_dispatch_policy_consumer_contract_guard.py`

**Implementation checklist:**

- import `validate_policy_bool_schema`;
- define a single tuple for adapter manifest policy snapshot keys;
- build `policy_snapshot` from typed literal values, defaulting invalid/missing keys to `False` only after recording schema errors;
- compute `dispatch_allowed = schema.ok and schema.values["allow_real_adapter_dispatch"] is True`;
- always keep `start_real_agent_process` forbidden when schema invalid or dispatch is not literal true;
- add `config_contract_valid` and `config_contract_errors` to the manifest.

**Verification:**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg/test_d537_dispatch_policy_consumer_contract_guard.py tests/aiwg/runners/test_b6_real_adapter_restricted_design.py -p no:cacheprovider
```

### Task 3: RED operator approval rejects non-literal dispatch policy before authorization

**Objective:** Prove `approve_real_start(...)` does not treat `"false"` as approval to continue.

**Files:**

- Modify test: `tests/aiwg/test_d537_dispatch_policy_consumer_contract_guard.py`
- Future modify: `aiwg/operator_approval.py`
- Fixture reference: `tests/aiwg/runners/test_b20_readiness_and_real_start_lifecycle.py`

**Test sketch:**

```python
from aiwg.operator_approval import approve_real_start


def test_d537_approve_real_start_rejects_non_literal_dispatch_policy(tmp_path: Path) -> None:
    config, _db_path, _approval_id, plan_path, report_path = prepare_plan_and_probe_chain(tmp_path)
    config["policy"]["allow_real_adapter_dispatch"] = "false"

    result = approve_real_start(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
        operator="alice",
        sandbox_plan_path=plan_path,
        sandbox_report_path=report_path,
        ttl_minutes=60,
        reason="D5.3.7 malformed dispatch policy must fail closed",
    )

    assert result.status == "policy_denied"
    assert any("config_contract_invalid" in reason for reason in result.policy_reasons)
    assert any("policy.allow_real_adapter_dispatch" in reason for reason in result.policy_reasons)
    assert result.authorization_path is None
```

Do not import test helpers across test modules if the project style discourages it; copying a tiny local fixture into the new D5.3.7 test file is acceptable.

**Expected RED:** current code can pass the direct dispatch check because `bool("false") is True`.

### Task 4: GREEN operator approval dispatch helper

**Objective:** Add a narrow dispatch-policy schema guard to `approve_real_start(...)` while preserving existing literal false behavior.

**Files:**

- Modify: `aiwg/operator_approval.py`
- Test: `tests/aiwg/test_d537_dispatch_policy_consumer_contract_guard.py`

**Implementation checklist:**

- import `validate_policy_bool_schema`;
- add a tiny helper for `allow_real_adapter_dispatch` literal-bool validation;
- if schema invalid: return `RealStartAuthorizationResult(status="policy_denied", error="; ".join(reasons), policy_reasons=reasons)`;
- if literal false: keep existing `status="blocked"` and `error="allow_real_adapter_dispatch=false"`;
- if literal true: continue to readiness/authorization checks exactly as before.

**Verification:**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg/test_d537_dispatch_policy_consumer_contract_guard.py tests/aiwg/runners/test_b20_readiness_and_real_start_lifecycle.py -p no:cacheprovider
```

### Task 5: RED resume path rejects non-literal dispatch policy before real-mode continuation

**Objective:** Prove `resume_preflight(...)` also fails closed for malformed `allow_real_adapter_dispatch` and does not fall through to unrelated real-start authorization errors.

**Files:**

- Modify test: `tests/aiwg/test_d537_dispatch_policy_consumer_contract_guard.py`
- Future modify: `aiwg/operator_approval.py`

**Test sketch:**

```python
from aiwg.operator_approval import resume_preflight


def test_d537_resume_preflight_rejects_non_literal_dispatch_policy(tmp_path: Path) -> None:
    config, _db_path, _approval_id, _plan_path, _report_path = prepare_plan_and_probe_chain(tmp_path)
    config["policy"]["real_adapter_execution_mode"] = "real"
    config["policy"]["allow_real_adapter_dispatch"] = 0

    result = resume_preflight(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
    )

    assert result.status == "policy_denied"
    assert any("config_contract_invalid" in reason for reason in result.policy_reasons)
    assert any("policy.allow_real_adapter_dispatch" in reason for reason in result.policy_reasons)
```

**Expected RED:** current code accepts `0` as a falsy dispatch block without reporting schema invalidity, or accepts `"false"` as truthy and falls through to later gates.

### Task 6: GREEN resume path uses same dispatch helper

**Objective:** Reuse the same helper in `resume_preflight(...)` and record a clear denied event for schema failures.

**Files:**

- Modify: `aiwg/operator_approval.py`
- Test: `tests/aiwg/test_d537_dispatch_policy_consumer_contract_guard.py`

**Implementation checklist:**

- call the dispatch-policy helper before the existing literal false block;
- for schema invalid, record `_record_resume_event(... event_type="preflight_resume_denied", payload={"reason": "runtime_policy_denied", "reasons": reasons, ...})` or an equivalent existing denied-event shape;
- return `PreflightResumeResult(status="policy_denied", error="; ".join(reasons), policy_reasons=reasons)`;
- for literal false, keep existing `preflight_resume_blocked` / `real_dispatch_blocked` behavior and `error="allow_real_adapter_dispatch=false"`.

### Task 7: Preserve literal true/false fixture behavior

**Objective:** Make sure strict schema validation does not accidentally turn D5.3.7 into a real-execution authorization change.

**Files:**

- Test: existing B6/B20 tests plus the new D5.3.7 file.

**Verification commands:**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/aiwg/test_d537_dispatch_policy_consumer_contract_guard.py \
  tests/aiwg/runners/test_b6_real_adapter_restricted_design.py \
  tests/aiwg/runners/test_b20_readiness_and_real_start_lifecycle.py \
  tests/aiwg/test_d536_runtime_policy_contract_guard.py \
  -p no:cacheprovider
```

Then broader safe regression:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg -p no:cacheprovider
```

## Acceptance criteria for D5.3.7 implementation

Future D5.3.7 implementation can be marked `completed_ready_for_codex_review` only when all are true:

- RED tests were observed before runtime fixes.
- `adapter_registry` no longer uses Python truthiness to decide `dispatch_allowed` or typed manifest `policy_snapshot` values.
- `operator_approval.approve_real_start(...)` and `resume_preflight(...)` reject malformed/non-literal `allow_real_adapter_dispatch` with `config_contract_invalid` reasons.
- Literal `False` keeps existing blocked semantics.
- Literal `True` is not rejected by schema alone; later gates still block/authorize as before.
- No `aiwg.yaml` safety switch was changed.
- MCP still exposes only read-only tools.
- No protected AIVideoTrans business-repository files were written.
- No real process, push, merge, deploy, GitHub write, or CodeX Automation modification occurred.
- Targeted D5.3.7 + D5.3.6/B6/B20 regressions pass.
- Full `tests/aiwg` suite passes.

## CodeX review checklist

Ask CodeX to verify specifically:

- `"false"` can no longer make `dispatch_allowed=True`.
- `0` / `1` / missing / non-mapping policy are schema errors, not accepted policy values.
- The D5.3.5 safe-default validator remains distinct from runtime literal-bool schema validation.
- D5.3.7 did not expand scope to `git_steward`, `d5_preflight`, MCP mutation tools, or business repo writes.
- Existing B20 explicit-true fixtures remain usable as authorization lifecycle tests without starting real task processes.

## Recommended next after D5.3.7 planning review

If CodeX approves this planning slice, proceed to D5.3.7 implementation with strict TDD in the task order above. Do not proceed to D5.3.8 or any real-agent / mutation-tool enablement until the D5.3.7 implementation artifact itself is reviewed and accepted.
