# AIWG D5.3.9 D5 Preflight Policy Consumer Strict-Config Alignment Plan

> **For Hermes:** This is a planning-only gate. Do not implement D5.3.9 until this planning artifact passes CodeX review and the user explicitly authorizes implementation. Future implementation must use strict TDD and keep fake/dry-run/preflight-only safety boundaries.

**Goal:** Plan a narrow D5.3.9 hardening slice so `aiwg.d5_preflight.evaluate_d5_preflight()` rejects malformed, missing, non-mapping, or non-literal boolean policy values before D5 preflight can interpret forbidden mutation switches through Python truthiness or crash during setup.

**Architecture:** Keep D5 preflight as a dry-run/fake evidence gate. Reuse `aiwg.config.validate_policy_bool_schema()` for `policy.*` forbidden action switches and add only a local optional top-level legacy switch guard inside `aiwg/d5_preflight.py`. For malformed config, fail closed as `status="blocked"` with `config_contract_invalid` in `policy_denials` before DB setup or D5.1 component evaluation can crash or normalize the malformed shape.

**Tech Stack:** Python 3.11, pytest, SQLite control-plane state, YAML config, existing AIWG CLI `d5-preflight`, existing D5 preflight tests. No real agents, no MCP mutation tools, no GitHub writes, no target repository writes, no commit/push/merge/deploy.

---

## Planning status

```text
phase = D5.3.9-d5-preflight-policy-consumer-strict-config-alignment-planning
status = completed_ready_for_codex_review
implementation_status = not_started
```

This document follows D5.3.8 implementation CodeX review passing. It intentionally stops at planning and does not authorize runtime/test/config changes.

## Upstream gate

D5.3.8 implementation acceptance is reviewed:

```text
docs/ai-workgroup/state/artifacts/phase-d5-3-8-git-steward-policy-consumer-strict-config-alignment/acceptance.json
status = codex_review_passed
codex_review.status = passed
codex_review.passed = true
```

D5.3.8 recommended next step was:

```text
Proceed to D5.3.9 planning-only for the next remaining policy-consumer strict-config-alignment slice.
```

## Why D5 preflight is selected for D5.3.9

D5.3.6 aligned the central `aiwg.policy.evaluate_runtime_policy()` consumer. D5.3.7 aligned `adapter_registry` / `operator_approval` for `allow_real_adapter_dispatch`. D5.3.8 aligned Git Steward mutation-policy denials.

Remaining direct policy-truthiness scan still shows several consumers:

```text
aiwg/d5_preflight.py:676       if bool(policy.get(key))
aiwg/real_adapter_process.py   allow_real_process_execution / allow_secret_access / allow_network_write
aiwg/real_adapter_executor.py  adapter_output_handoff / allow_secret_access / allow_network_write
aiwg/real_adapter_sandbox.py   allow_secret_access
aiwg/runners/orchestrator.py   return bool(policy.get(key))
aiwg/doctor.py                messaging-only _as_bool paths after config validator
```

Select `aiwg/d5_preflight.py` now because:

1. CodeX explicitly suggested `d5_preflight` as an appropriate next candidate.
2. D5 preflight is the control-plane evidence gate immediately below future real-agent/protected-write gates.
3. The risk is isolated to `_policy_denials()` plus its call ordering in `evaluate_d5_preflight()`.
4. Implementation can remain a small single-runtime-file slice and does not require enabling real agents or touching MCP/GitHub/target repositories.
5. It preserves the incremental D5.3.x pattern: strict literal-bool consumer alignment, not repo-wide policy migration.

## Current code reconnaissance

### Current D5 forbidden switch set

`aiwg/d5_preflight.py:34-46` currently defines:

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

This matches `aiwg.config.POLICY_FORBIDDEN_FALSE_KEYS` at planning time. Do not broaden this set in D5.3.9 unless CodeX explicitly asks; `allow_network_write` remains false in `aiwg.yaml`, but is not a current D5 preflight `_policy_denials()` consumer.

### Current D5 preflight policy consumer

`aiwg/d5_preflight.py:66-154` runs `init_database(...)` before `_policy_denials(config)`:

```python
db_path = init_database(config=config, project_root=project_root_path)
policy_denials = _policy_denials(config)
```

`aiwg/d5_preflight.py:670-678` currently uses truthiness:

```python
def _policy_denials(config: dict[str, Any]) -> list[str]:
    denials: list[str] = []
    policy = config.get("policy") if isinstance(config.get("policy"), dict) else {}
    for key in FORBIDDEN_POLICY_KEYS:
        if bool(config.get(key)):
            denials.append(key)
        if bool(policy.get(key)):
            denials.append(f"policy.{key}")
    return denials
```

Risk points:

- `policy = []` or `policy = None` is silently treated as `{}` inside `_policy_denials()`.
- But `init_database()` currently runs before `_policy_denials()` and `aiwg.state.database._upsert_agent_capabilities()` uses `policy.get(...)`, so non-mapping policy can crash before D5 preflight can return a structured blocked snapshot.
- `policy.allow_write = "false"` is blocked only by accidental truthiness and is labeled as `policy.allow_write`, not `config_contract_invalid`.
- `policy.allow_write = 0` or missing `policy.allow_write` can be silently accepted as safe instead of rejected as a malformed/missing required policy contract.
- Legacy top-level switches such as `allow_write = "false"` are currently interpreted with `bool(config.get(key))`; if a top-level compatibility switch is present, it should also be literal bool.

## Scope

### In scope for future D5.3.9 implementation

Default future implementation may touch only:

```text
aiwg/d5_preflight.py
tests/aiwg/preflight/test_d539_d5_preflight_policy_consumer_contract_guard.py
docs/ai-workgroup/state/artifacts/phase-d5-3-9-d5-preflight-policy-consumer-strict-config-alignment/acceptance.json
```

Implementation design should reuse existing `aiwg.config.validate_policy_bool_schema()` from `aiwg/d5_preflight.py`. Do not create a new shared helper unless CodeX explicitly requests it.

### Planning-only files created by this step

```text
docs/plans/2026-06-08-aiwg-d5-3-9-d5-preflight-policy-consumer-strict-config-alignment-plan.md
docs/ai-workgroup/state/artifacts/phase-d5-3-9-d5-preflight-policy-consumer-strict-config-alignment-planning/acceptance.json
```

### Out of scope

Do **not** modify or enable:

```text
aiwg.yaml
aiwg/config.py unless CodeX requests shared helper changes
aiwg/state/database.py unless CodeX explicitly accepts widening beyond the early-return design
aiwg/real_adapter_process.py
aiwg/real_adapter_executor.py
aiwg/real_adapter_sandbox.py
aiwg/runners/orchestrator.py
aiwg/doctor.py
aiwg/mcp/*
D:/example/protected-business-repo
CodeX Automations
GitHub PR/comment/write APIs
real agent adapters
real adapter process execution
real Git commit / push / merge / PR creation
deploy
```

Do not interpret D5.3.9 as authorization to run D5 real execution. It is a fail-closed config-consumer hardening slice only.

## Expected behavior contract

### Schema-valid literal `False`

For all `FORBIDDEN_POLICY_KEYS` in `policy`:

```yaml
policy:
  allow_write: false
  allow_real_agents: false
  allow_real_adapter_dispatch: false
  allow_real_process_execution: false
  allow_push: false
```

Expected:

```text
snapshot.status = passed_dry_run, unless another D5.1 component such as budget or external-review fixture blocks
snapshot.policy_denials does not include config_contract_invalid
all safety flags remain false
artifact path remains under docs/ai-workgroup/state/artifacts/phase-d5-preflight/
protected target repo is not written
```

### Schema-valid literal `True`

Preserve existing denial strings for explicit true values:

```text
policy.allow_real_agents = true  -> policy_denials includes "policy.allow_real_agents"
policy.allow_push = true         -> policy_denials includes "policy.allow_push"
top-level allow_write = true     -> policy_denials includes "allow_write"
```

Literal `True` remains schema-valid but blocked; it does not authorize real execution.

### Schema-invalid policy section or values

Fail closed as config contract invalid:

```text
policy = []
policy = null
missing policy.allow_write
policy.allow_write = "false"
policy.allow_write = 0
policy.allow_write = []
```

Expected:

```text
snapshot.status = blocked
snapshot.policy_denials includes "config_contract_invalid: ..."
ready_for_real_agent_execution = false
ready_for_protected_business_repository_write = false
target_writes_performed = false
mcp_mutation_tools_exposed = false
github_write_api_called = false
pr_mutation_performed = false
codex_automation_modified = false
real_agents_started = false
real_processes_started = false
```

For non-mapping `policy`, D5.3.9 should fail closed before `init_database(...)` or D5.1 component evaluation can dereference `policy.get(...)` indirectly.

### Optional legacy top-level forbidden switches

Top-level forbidden keys are not required because current `aiwg.yaml` stores them under `policy`. Preserve backwards compatibility:

```text
allow_write absent       -> accepted; no top-level denial
allow_write = false      -> accepted; no top-level denial
allow_write = true       -> existing top-level denial "allow_write"
allow_write = "false"   -> config_contract_invalid
allow_write = 0          -> config_contract_invalid
```

Use `type(value) is bool`, not `bool(value)` and not `isinstance(value, bool)`.

## Recommended implementation design after CodeX approval

### Helper shape

In `aiwg/d5_preflight.py`, import the existing schema helper:

```python
from aiwg.config import POLICY_FORBIDDEN_FALSE_KEYS, validate_policy_bool_schema
```

Optionally make the local constant share the same tuple to prevent drift:

```python
FORBIDDEN_POLICY_KEYS = POLICY_FORBIDDEN_FALSE_KEYS
```

Add a local top-level compatibility guard:

```python
def _top_level_policy_contract_denials(config: dict[str, Any]) -> list[str]:
    denials: list[str] = []
    for key in FORBIDDEN_POLICY_KEYS:
        if key not in config:
            continue
        value = config[key]
        if type(value) is not bool:
            denials.append(
                f"config_contract_invalid: {key} must be literal bool when present; got {type(value).__name__}"
            )
    return denials
```

Replace `_policy_denials()` with schema-first logic:

```python
def _policy_denials(config: dict[str, Any]) -> list[str]:
    schema = validate_policy_bool_schema(config, required_keys=FORBIDDEN_POLICY_KEYS)
    contract_denials = [f"config_contract_invalid: {error}" for error in schema.errors]
    contract_denials.extend(_top_level_policy_contract_denials(config))
    if contract_denials:
        return contract_denials

    denials: list[str] = []
    for key in FORBIDDEN_POLICY_KEYS:
        if key in config and config[key] is True:
            denials.append(key)
        if schema.values[key] is True:
            denials.append(f"policy.{key}")
    return denials
```

Add a tiny predicate:

```python
def _has_config_contract_denial(denials: list[str]) -> bool:
    return any(reason.startswith("config_contract_invalid:") for reason in denials)
```

### Call-order guard

Move `_policy_denials(config)` before `init_database(...)` in `evaluate_d5_preflight()`.

If `_has_config_contract_denial(policy_denials)` is true, return a deterministic blocked snapshot without calling `init_database(...)` or `_evaluate_d5_1_components(...)`. Prefer writing an orchestrator-only artifact if the artifact path can be safely resolved; otherwise return JSON/text with `artifact_path = None` and `artifact_write_performed = false`. Do not touch the target root.

CodeX should explicitly decide whether invalid-config D5 preflight must still write SQLite rows. Default recommendation: **do not widen D5.3.9 into `aiwg/state/database.py`**; fail closed before DB setup for invalid config, mirroring D5.3.8 Git Steward's early malformed-config denial.

## Future TDD task breakdown

### Task 1: RED tests for malformed `policy` section and required keys

**Objective:** Prove D5 preflight currently accepts/misclassifies malformed policy schema or crashes before structured denial.

**Files:**

- Create: `tests/aiwg/preflight/test_d539_d5_preflight_policy_consumer_contract_guard.py`
- Future modify: `aiwg/d5_preflight.py`

**Test cases:**

```python
@pytest.mark.parametrize("policy_value", [None, [], ["not", "mapping"]])
def test_d539_policy_section_must_be_mapping(policy_value, tmp_path):
    config = build_test_config(project_root)
    config["policy"] = policy_value
    snapshot = evaluate_d5_preflight(...)
    assert snapshot["status"] == "blocked"
    assert any("config_contract_invalid" in reason for reason in snapshot["policy_denials"])
    assert any("policy must be a mapping" in reason for reason in snapshot["policy_denials"])
    assert_d5_safety_flags_false(snapshot)
```

```python
@pytest.mark.parametrize("value", ["false", "true", 0, 1, []])
def test_d539_policy_forbidden_keys_require_literal_bool(value, tmp_path):
    config = build_test_config(project_root)
    config["policy"]["allow_write"] = value
    snapshot = evaluate_d5_preflight(...)
    assert snapshot["status"] == "blocked"
    assert any("config_contract_invalid" in reason for reason in snapshot["policy_denials"])
    assert any("policy.allow_write" in reason for reason in snapshot["policy_denials"])
```

```python
def test_d539_missing_policy_forbidden_key_is_config_contract_invalid(tmp_path):
    config = build_test_config(project_root)
    del config["policy"]["allow_write"]
    snapshot = evaluate_d5_preflight(...)
    assert snapshot["status"] == "blocked"
    assert any("config_contract_invalid" in reason for reason in snapshot["policy_denials"])
    assert any("policy.allow_write" in reason for reason in snapshot["policy_denials"])
```

Expected RED before implementation:

```text
policy=None/list may crash before structured snapshot or pass as empty policy
policy.allow_write="false" blocks for the wrong reason: "policy.allow_write" instead of config_contract_invalid
policy.allow_write=0 and missing allow_write may pass_dry_run instead of blocked
```

### Task 2: RED tests for optional top-level compatibility keys

**Objective:** Preserve legacy top-level literal bool behavior while rejecting top-level truthiness/coercion.

**Test cases:**

```python
def test_d539_top_level_false_and_absent_remain_compatible(tmp_path):
    config = build_test_config(project_root)
    snapshot_absent = evaluate_d5_preflight(...)
    config["allow_write"] = False
    snapshot_false = evaluate_d5_preflight(...)
    assert snapshot_absent["status"] == "passed_dry_run"
    assert snapshot_false["status"] == "passed_dry_run"
```

```python
def test_d539_top_level_true_preserves_existing_denial(tmp_path):
    config = build_test_config(project_root)
    config["allow_write"] = True
    snapshot = evaluate_d5_preflight(...)
    assert snapshot["status"] == "blocked"
    assert "allow_write" in snapshot["policy_denials"]
```

```python
@pytest.mark.parametrize("value", ["false", 0, 1, []])
def test_d539_top_level_forbidden_key_if_present_must_be_literal_bool(value, tmp_path):
    config = build_test_config(project_root)
    config["allow_write"] = value
    snapshot = evaluate_d5_preflight(...)
    assert snapshot["status"] == "blocked"
    assert any("config_contract_invalid" in reason for reason in snapshot["policy_denials"])
    assert any("allow_write" in reason for reason in snapshot["policy_denials"])
```

### Task 3: RED CLI test for `d5-preflight --dry-run --json --fail-on-blocked`

**Objective:** Prove the CLI returns a machine-readable blocked result for malformed policy config.

**Command shape:**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli d5-preflight \
  --config <tmp>/orchestrator/aiwg.yaml \
  --workflow-id apf-preview-funnel \
  --target-root <tmp>/AIVideoTrans \
  --dry-run \
  --json \
  --fail-on-blocked
```

Use a config where:

```yaml
policy:
  allow_write: 0
```

Expected after implementation:

```text
exit_code = 3
payload.status = blocked
payload.policy_denials contains config_contract_invalid and policy.allow_write
all safety flags false
```

Expected RED before implementation:

```text
exit_code = 0 or payload.status = passed_dry_run because 0 is accepted by falsey coercion
```

### Task 4: GREEN implementation in `aiwg/d5_preflight.py`

**Objective:** Add the smallest schema-first policy-denial guard.

**Steps:**

1. Import `POLICY_FORBIDDEN_FALSE_KEYS` and `validate_policy_bool_schema`.
2. Align `FORBIDDEN_POLICY_KEYS` to `POLICY_FORBIDDEN_FALSE_KEYS` if CodeX agrees this is acceptable and does not broaden the tuple.
3. Add `_top_level_policy_contract_denials(...)`.
4. Replace `_policy_denials(...)` to return `config_contract_invalid` first and then use `is True` for literal true denials.
5. Move policy contract evaluation before DB initialization in `evaluate_d5_preflight()`.
6. Add an early blocked snapshot path for `config_contract_invalid` so non-mapping policy cannot crash through DB setup.
7. Preserve current literal true denial strings and D5.0/D5.1 blocked behavior for budget/external-review fixture denials.

### Task 5: Regression verification

Run targeted tests:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/aiwg/preflight/test_d539_d5_preflight_policy_consumer_contract_guard.py \
  tests/aiwg/preflight/test_d50_preflight.py \
  tests/aiwg/preflight/test_d51_preflight.py \
  tests/aiwg/preflight/test_d52_preflight_hardening.py \
  tests/aiwg/test_d535_policy_safety_config_validator.py \
  -p no:cacheprovider
```

Then run safety checks:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli doctor --config aiwg.yaml
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.mcp.server --config aiwg.yaml --list-tools
```

If targeted tests pass, run full AIWG suite:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg -p no:cacheprovider
```

### Task 6: Future implementation acceptance artifact

After implementation, write only:

```text
docs/ai-workgroup/state/artifacts/phase-d5-3-9-d5-preflight-policy-consumer-strict-config-alignment/acceptance.json
```

Expected status before CodeX review:

```json
{
  "status": "completed_ready_for_codex_review",
  "codex_review": {
    "status": "pending",
    "passed": null
  }
}
```

Record RED/GREEN evidence, targeted/full regression, doctor, MCP read-only tool surface, business-repo marker scan, and safety boundary flags.

## Safety boundary for D5.3.9

D5.3.9 must keep these false / unavailable:

```text
real_agents_enabled = false
external_agents_enabled = false
real_adapter_process_execution_enabled = false
mcp_mutation_tools_exposed = false
protected_business_repository_write_performed = false
github_write_or_pr_mutation_performed = false
git_commit_performed = false
git_push_performed = false
git_merge_performed = false
deploy_performed = false
codex_automation_modified = false
aiwg_yaml_modified = false
```

Allowed writes in this planning step are limited to the planning document and planning acceptance artifact under the orchestrator repository.

## CodeX review questions

Ask CodeX to review:

1. Is `d5_preflight` the right next D5.3.9 consumer after D5.3.8?
2. Should D5.3.9 default implementation scope stay limited to `aiwg/d5_preflight.py` plus one new test file, or should non-mapping policy crash avoidance explicitly include `aiwg/state/database.py` hardening?
3. Should invalid config preflight write an orchestrator artifact while skipping DB rows, or return a no-artifact blocked snapshot before setup?
4. Is the optional top-level legacy switch contract correct: absent accepted, literal bool accepted, non-literal rejected?
5. Should D5 preflight continue to use only existing `FORBIDDEN_POLICY_KEYS`, or should CodeX require adding `allow_network_write` to the D5 preflight forbidden set in a separate reviewed slice?

## Recommended next after this planning artifact

Submit this D5.3.9 planning-only artifact to CodeX review. If CodeX passes, wait for explicit user authorization before starting D5.3.9 implementation. Do not enable real agents, MCP mutation tools, protected writes, GitHub/PR mutation, commit, push, merge, deploy, or CodeX Automation changes.
