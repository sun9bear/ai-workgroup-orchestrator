# AIWG D5.3.10 Real Adapter Policy Consumer Strict-Config Alignment Plan

> **For Hermes:** This is a planning-only gate. Do not implement D5.3.10 until this planning artifact passes CodeX review **and** Human/CodeX explicitly authorizes implementation. The target files are close to real adapter execution boundaries, so this plan must not edit runtime code, tests, `aiwg.yaml`, MCP tools, CodeX Automations, Git/GitHub state, or the protected AIVideoTrans business repository.

**Goal:** Plan a narrow D5.3.10 hardening slice so `aiwg.real_adapter_process`, `aiwg.real_adapter_executor`, and `aiwg.real_adapter_sandbox` stop making real-adapter boundary decisions from Python truthiness or malformed `policy` shapes.

**Architecture:** Reuse the existing literal-bool schema helper `aiwg.config.validate_policy_bool_schema()` at the real-adapter consumer edge, before process-start, environment-injection, output-handoff, or sandbox-plan readiness decisions. Preserve literal `False` / literal `True` semantics already used by explicit test fixtures, but fail closed with `config_contract_invalid` for non-mapping policy sections and non-literal policy boolean values. Keep all real agents, protected writes, MCP mutation tools, GitHub writes, commits, pushes, merges, deploys, and CodeX Automation changes disabled.

**Tech Stack:** Python 3.11, pytest, SQLite control-plane state, existing AIWG real-adapter dry-run/sandbox/probe modules, existing `validate_policy_bool_schema`, existing B8/B10/B11 runner tests, acceptance artifacts under `docs/ai-workgroup/state/artifacts/`.

---

## Planning status

```text
phase = D5.3.10-real-adapter-policy-consumer-strict-config-alignment-planning
status = completed_ready_for_codex_review
implementation_status = not_started
```

This document follows D5.3.9 implementation CodeX review passing. It intentionally stops at planning and does not authorize implementation.

## Upstream gate

D5.3.9 implementation acceptance is reviewed:

```text
docs/ai-workgroup/state/artifacts/phase-d5-3-9-d5-preflight-policy-consumer-strict-config-alignment/acceptance.json
status = codex_review_passed
codex_review.status = passed
codex_review.passed = true
```

D5.3.9 recommended next step was:

```text
Proceed only with D5.3.10 planning-only for strict policy consumer closure around real_adapter_process / real_adapter_executor / real_adapter_sandbox; do not implement until explicitly authorized because these paths are closer to real execution boundaries.
```

## Why these consumers are selected for D5.3.10

D5.3.6 aligned the central runtime policy entrypoint. D5.3.7 aligned real-adapter dispatch checks in `adapter_registry` / `operator_approval`. D5.3.8 aligned Git Steward mutation policy checks. D5.3.9 aligned D5 preflight `_policy_denials()`.

The remaining high-risk direct policy consumers closest to real adapter execution are now concentrated in:

```text
aiwg/real_adapter_process.py
aiwg/real_adapter_executor.py
aiwg/real_adapter_sandbox.py
```

These files control or describe:

1. whether a supervised sandbox probe process may start;
2. whether environment keys can be exposed to adapter subprocesses or dry-run reports;
3. whether adapter output handoff may mutate task state or run verification commands;
4. whether sandbox invocation plans are marked ready near the real execution boundary.

Because these paths are close to process execution and handoff state mutation, D5.3.10 must be planning-only first.

## Current code reconnaissance

### Existing strict helper

`aiwg/config.py:225-249` already provides the reusable type/schema layer:

```python
def validate_policy_bool_schema(config: Config, *, required_keys: Iterable[str]) -> PolicyBoolSchemaResult:
    """Validate runtime-consumed policy booleans without enforcing safe-default values."""
```

Contract to preserve:

- `policy` must be a mapping;
- each selected required key must be present;
- every selected value must satisfy `type(value) is bool`;
- the helper does **not** enforce safe-default values such as `allow_* = False`.

### `aiwg/real_adapter_process.py`

Observed consumers:

```text
aiwg/real_adapter_process.py:73   policy = config.get("policy") or {}
aiwg/real_adapter_process.py:117  if not bool(policy.get("allow_real_process_execution", False))
aiwg/real_adapter_process.py:697  policy = config.get("policy") or {}
aiwg/real_adapter_process.py:707  if bool(policy.get("allow_secret_access", False)) or not _looks_secret_key(key)
aiwg/real_adapter_process.py:714  "secret_access_allowed": bool(policy.get("allow_secret_access", False))
aiwg/real_adapter_process.py:715  "network_write_allowed": bool(policy.get("allow_network_write", False))
```

Risk examples:

- `policy.allow_real_process_execution = "false"` is truthy and can pass the explicit process-execution gate.
- `policy.allow_real_process_execution = 0` is treated as false but accepted silently instead of reported as schema-invalid.
- `policy.allow_secret_access = "false"` is truthy and can allow secret-looking allowlisted keys in the sandbox environment contract.
- `policy.allow_network_write = "false"` reports network-write allowed in process metadata even though the value is not a literal boolean.
- Non-empty non-mapping policy values such as `policy = ["not", "mapping"]` can raise `AttributeError` at `.get(...)` instead of producing a structured block.

### `aiwg/real_adapter_executor.py`

Observed consumers:

```text
aiwg/real_adapter_executor.py:88   "handoff_allowed": bool((config.get("policy") or {}).get("adapter_output_handoff", False))
aiwg/real_adapter_executor.py:168  if bool((config.get("policy") or {}).get("adapter_output_handoff", False)):
aiwg/real_adapter_executor.py:216  policy = config.get("policy") or {}
aiwg/real_adapter_executor.py:223  "secret_access_allowed": bool(policy.get("allow_secret_access", False))
aiwg/real_adapter_executor.py:224  "network_write_allowed": bool(policy.get("allow_network_write", False))
```

Risk examples:

- `policy.adapter_output_handoff = "false"` is truthy and can call `apply_adapter_output_handoff(...)`, which may record adapter-output events, mark tasks, or run verification commands.
- `policy.adapter_output_handoff = 0` is accepted silently instead of being reported as an invalid policy contract.
- `policy.allow_secret_access = "false"` / `policy.allow_network_write = "false"` make the dry-run environment contract report capability flags through truthiness.
- Non-mapping `policy` can crash through `(config.get("policy") or {}).get(...)` when the non-mapping object is truthy.

Important compatibility note:

`build_default_config()` includes `policy.adapter_output_handoff = False`, but the current checked-in `aiwg.yaml` does **not** contain `adapter_output_handoff` or `real_adapter_execution_mode`. D5.3.10 should not modify `aiwg.yaml` during planning. Future implementation must either:

1. keep `adapter_output_handoff` absent-compatible with a strict optional-bool rule (`absent -> False`, present value must be literal bool); or
2. explicitly ask CodeX/Human to widen implementation scope to add safe defaults to `aiwg.yaml`.

Default D5.3.10 recommendation: use option 1 to keep the implementation slice in the three target runtime files only.

### `aiwg/real_adapter_sandbox.py`

Observed consumers:

```text
aiwg/real_adapter_sandbox.py:208  policy = config.get("policy") or {}
aiwg/real_adapter_sandbox.py:209  default_minutes = int(policy.get("default_timeout_minutes") or 30)
aiwg/real_adapter_sandbox.py:225  policy = config.get("policy") or {}
aiwg/real_adapter_sandbox.py:239  "secret_access_allowed": bool(policy.get("allow_secret_access", False))
```

Risk examples:

- Non-mapping `policy` can crash in `_bounded_timeout_seconds(...)` before the sandbox plan can return a structured blocked result.
- `policy.allow_secret_access = "false"` is truthy and makes a sandbox invocation plan report secret access allowed.
- `policy.allow_secret_access = 0` is accepted silently instead of being reported as schema-invalid.

`default_timeout_minutes` is a numeric policy value, not a D5.3.10 mutation boolean consumer. D5.3.10 should only ensure a non-mapping policy shape is caught before `_bounded_timeout_seconds(...)`; numeric timeout schema migration should remain out of scope unless CodeX explicitly asks to widen the slice.

## Selected D5.3.10 implementation surface after future authorization

Default future implementation may touch only:

```text
aiwg/real_adapter_process.py
aiwg/real_adapter_executor.py
aiwg/real_adapter_sandbox.py
tests/aiwg/runners/test_d5310_real_adapter_policy_consumer_contract_guard.py
docs/ai-workgroup/state/artifacts/phase-d5-3-10-real-adapter-policy-consumer-strict-config-alignment/acceptance.json
```

Planning-only files created by this step:

```text
docs/plans/2026-06-08-aiwg-d5-3-10-real-adapter-policy-consumer-strict-config-alignment-plan.md
docs/ai-workgroup/state/artifacts/phase-d5-3-10-real-adapter-policy-consumer-strict-config-alignment-planning/acceptance.json
```

Optional only if CodeX explicitly requests scope widening:

```text
aiwg/config.py                      # only if optional policy-bool helper should be centralized
aiwg.yaml                           # only if adapter_output_handoff / real_adapter_execution_mode safe defaults must be added
aiwg/operator_approval.py           # only if real_adapter_execution_mode string schema is moved into this slice
aiwg/adapter_output.py              # only if adapter_result.handoff_allowed parsing is pulled into this slice
```

## Explicitly out of scope

Do **not** modify, enable, or run as part of D5.3.10 planning:

```text
real agents
external agents
MCP mutation tools
protected business repository writes
D:/example/protected-business-repo
GitHub write / PR mutation
git commit / push / merge
deploy
CodeX Automation modification
aiwg.yaml changes without explicit CodeX/Human scope widening
real_adapter_execution_mode real launch
real agent binaries such as opencode/claude/codex/hermes
```

Do not interpret D5.3.10 as authorization for real adapter execution. It is only a strict policy-consumer hardening plan.

## Intended behavior contract

### Shared schema rules

For the targeted real-adapter files:

```text
policy must be a mapping
consumed policy booleans must be literal bool when required or present
strings, integers, lists, objects, null, and non-mapping policy shapes must fail closed
```

Errors should be machine-readable and start with:

```text
config_contract_invalid:
```

### `real_adapter_process` contract

Required literal bool keys:

```text
policy.allow_real_process_execution
policy.allow_secret_access
policy.allow_network_write
```

Expected behavior:

- `allow_real_process_execution = False` preserves existing block:

```text
status = sandbox_process_blocked
error = allow_real_process_execution=false
started_real_process = false
real_agent_binary_started = false
agent_runs inserted = 0
```

- `allow_real_process_execution = True` remains schema-valid but does not bypass other gates:
  - real-agent binary block still applies;
  - cwd boundary still applies;
  - readiness-bound plan still applies;
  - existing harmless pytest probe fixtures may still run Python-only probes when future implementation verification is authorized.

- Malformed values such as `"false"`, `0`, `1`, `[]`, `{}` for the three required booleans must block before `subprocess.Popen(...)` and before any secret/network environment decision:

```text
status = sandbox_process_blocked
error startswith config_contract_invalid:
started_real_process = false
real_agent_binary_started = false
agent_runs inserted = 0
```

### `real_adapter_executor` contract

Required literal bool keys:

```text
policy.allow_secret_access
policy.allow_network_write
```

Optional absent-compatible literal bool key:

```text
policy.adapter_output_handoff
```

`adapter_output_handoff` compatibility rule:

```text
absent -> treated as False for current aiwg.yaml compatibility
present literal False -> no handoff
present literal True -> preserve existing explicit handoff behavior
present non-literal -> config_contract_invalid and no handoff
```

Expected malformed behavior:

- never call `apply_adapter_output_handoff(...)` when `adapter_output_handoff` is schema-invalid;
- never report `secret_access_allowed = true` or `network_write_allowed = true` from non-literal values;
- record or expose `config_contract_invalid` in dry-run output/event metadata;
- keep `started_real_process = false` and no external agent process.

If CodeX wants a stronger fail-closed signal, future implementation may return a new status such as:

```text
dry_run_policy_denied
```

but it should avoid broad dataclass/API churn unless the RED tests require it. The minimum acceptable closure is: no handoff, no secret/network capability by truthiness, machine-readable `config_contract_invalid` metadata.

### `real_adapter_sandbox` contract

Required literal bool key:

```text
policy.allow_secret_access
```

Policy shape rule:

```text
policy must be a mapping before _bounded_timeout_seconds(...) or _environment_contract(...) run
```

Expected behavior:

- `allow_secret_access = False` preserves existing plan-ready behavior with secret-looking keys blocked.
- `allow_secret_access = True` remains schema-valid for explicit fixtures, but values are still not recorded and real execution remains disabled.
- `allow_secret_access = "false"`, `0`, `1`, `[]`, or non-mapping `policy` returns a structured block:

```text
status = sandbox_invocation_blocked
error startswith config_contract_invalid:
started_real_process = false
execution_authorized = false
agent_runs inserted = 0
sandbox plan not marked ready
```

## Recommended implementation design after future authorization

### Shared formatting convention

Use the existing reason prefix style from D5.3.7 / D5.3.9:

```python
[f"config_contract_invalid: {error}" for error in schema.errors]
```

### Required bool helper pattern

Default: use `validate_policy_bool_schema()` directly in each target module with small local constants. Do not modify `aiwg/config.py` unless CodeX requests centralizing optional-key support.

Example future pattern:

```python
from aiwg.config import validate_policy_bool_schema

_REAL_ADAPTER_PROCESS_POLICY_KEYS = (
    "allow_real_process_execution",
    "allow_secret_access",
    "allow_network_write",
)

schema = validate_policy_bool_schema(config, required_keys=_REAL_ADAPTER_PROCESS_POLICY_KEYS)
if not schema.ok:
    reasons = [f"config_contract_invalid: {error}" for error in schema.errors]
```

### Optional bool helper for `adapter_output_handoff`

Because current `aiwg.yaml` omits `adapter_output_handoff`, D5.3.10 should not make it required unless the implementation scope also includes config alignment. Recommended local helper in `real_adapter_executor.py`:

```python
def _optional_policy_bool_reason(config: dict[str, Any], key: str) -> tuple[bool, list[str]]:
    policy = config.get("policy")
    if not isinstance(policy, dict):
        return False, ["config_contract_invalid: policy schema invalid: policy must be a mapping"]
    if key not in policy:
        return False, []
    value = policy[key]
    if type(value) is not bool:
        return False, [f"config_contract_invalid: policy.{key} must be literal bool; got {type(value).__name__}"]
    return value, []
```

This helper is shown only as implementation guidance; do not add it during planning.

### Ordering requirements

Future implementation must validate before side-effectful or boundary-adjacent decisions:

1. `real_adapter_process.run_supervised_sandbox_probe(...)`
   - validate process/env policy booleans before `subprocess.Popen(...)`;
   - preferably validate before `artifact_dir.mkdir(...)` when the block can be represented without output artifacts;
   - malformed config returns `_blocked(..., reason="config_contract_invalid: ...")`.
2. `real_adapter_executor.execute_real_adapter_dry_run(...)`
   - validate env policy booleans before writing stdout/report metadata;
   - validate `adapter_output_handoff` before computing stdout `handoff_allowed` or calling `apply_adapter_output_handoff(...)`;
   - malformed handoff policy must not call handoff.
3. `real_adapter_sandbox.prepare_sandbox_invocation_plan(...)`
   - validate policy mapping before `_bounded_timeout_seconds(...)`;
   - validate `allow_secret_access` before `_environment_contract(...)`;
   - malformed config blocks plan readiness instead of writing a ready plan.

## Future strict TDD task breakdown

### Task 1: RED tests for `real_adapter_executor` handoff policy

**Objective:** Prove malformed `policy.adapter_output_handoff` no longer uses Python truthiness to trigger adapter output handoff.

**Files:**

- Create: `tests/aiwg/runners/test_d5310_real_adapter_policy_consumer_contract_guard.py`
- Future target: `aiwg/real_adapter_executor.py`

**Test cases:**

```python
@pytest.mark.parametrize("bad_value", ["false", "true", 0, 1, [], {}])
def test_d5310_executor_rejects_non_literal_adapter_output_handoff(tmp_path, bad_value):
    config, db_path, manifest, manifest_path, task = build_executor_fixture(tmp_path)
    config["policy"]["adapter_output_handoff"] = bad_value

    result = execute_real_adapter_dry_run(...)

    assert result.status in {"dry_run_policy_denied", "dry_run_succeeded"}
    stdout = json.loads(result.stdout_path.read_text(encoding="utf-8"))
    assert stdout["adapter_result"]["handoff_allowed"] is False
    assert any("config_contract_invalid" in item for item in stdout.get("config_contract_errors", []))
    assert no_adapter_output_handoff_events(db_path)
```

**Expected RED:** current code treats `"false"` and `"true"` as truthy and can call `apply_adapter_output_handoff(...)`; malformed values are not reported as `config_contract_invalid`.

### Task 2: RED tests for `real_adapter_executor` environment policy

**Objective:** Prove `allow_secret_access` / `allow_network_write` cannot be derived from non-literal values.

**Test cases:**

```python
@pytest.mark.parametrize("key,bad_value", [
    ("allow_secret_access", "false"),
    ("allow_secret_access", 0),
    ("allow_network_write", "false"),
    ("allow_network_write", []),
])
def test_d5310_executor_environment_policy_requires_literal_bools(tmp_path, key, bad_value):
    config, db_path, manifest, manifest_path, task = build_executor_fixture(tmp_path)
    config["policy"][key] = bad_value

    result = execute_real_adapter_dry_run(...)

    stdout = json.loads(result.stdout_path.read_text(encoding="utf-8"))
    assert stdout["environment"]["secret_access_allowed"] is False
    assert stdout["environment"]["network_write_allowed"] is False
    assert contains_config_contract_invalid(stdout)
```

**Expected RED:** current code reports capability booleans through `bool(policy.get(...))`.

### Task 3: RED tests for `real_adapter_sandbox` policy shape and secret access

**Objective:** Prove sandbox plan readiness fails closed for non-mapping policy and non-literal `allow_secret_access`.

**Test cases:**

```python
@pytest.mark.parametrize("bad_policy", [None, [], ["not", "mapping"]])
def test_d5310_sandbox_blocks_non_mapping_policy_before_plan_ready(tmp_path, bad_policy):
    config, db_path, manifest_path, approval_id = create_sandbox_fixture(tmp_path)
    config["policy"] = bad_policy

    result = prepare_sandbox_invocation_plan(...)

    assert result.status == "sandbox_invocation_blocked"
    assert "config_contract_invalid" in str(result.error)
    assert result.plan_path is None or not result.plan_path.exists()
    assert no_agent_runs(db_path)
```

```python
@pytest.mark.parametrize("bad_value", ["false", 0, 1, [], {}])
def test_d5310_sandbox_secret_access_policy_requires_literal_bool(tmp_path, bad_value):
    config, db_path, manifest_path, approval_id = create_sandbox_fixture(tmp_path)
    config["policy"]["allow_secret_access"] = bad_value

    result = prepare_sandbox_invocation_plan(...)

    assert result.status == "sandbox_invocation_blocked"
    assert "config_contract_invalid" in str(result.error)
    assert no_agent_runs(db_path)
```

**Expected RED:** current code may crash on non-mapping policy or mark `secret_access_allowed` by truthiness.

### Task 4: RED tests for `real_adapter_process` process execution gate

**Objective:** Prove `allow_real_process_execution` cannot be bypassed with truthy strings and malformed values block before any process start.

**Test cases:**

```python
@pytest.mark.parametrize("bad_value", ["false", "true", 0, 1, [], {}])
def test_d5310_process_execution_policy_requires_literal_bool(tmp_path, bad_value):
    config, db_path, manifest_path, approval_id = create_probe_fixture(tmp_path)
    config["policy"]["allow_real_process_execution"] = bad_value

    result = run_supervised_sandbox_probe(...)

    assert result.status == "sandbox_process_blocked"
    assert "config_contract_invalid" in str(result.error)
    assert no_agent_runs(db_path)
    payload = latest_event_payload(db_path, "real_adapter_sandbox_process_blocked")
    assert payload["started_real_process"] is False
    assert payload["real_agent_binary_started"] is False
```

**Expected RED:** current code treats `"false"` as truthy and can proceed past the explicit `allow_real_process_execution` gate until another later gate blocks or a harmless test process starts.

### Task 5: RED tests for `real_adapter_process` environment policy

**Objective:** Prove secret/network environment decisions require literal booleans before a sandbox probe process starts.

**Test cases:**

```python
@pytest.mark.parametrize("key,bad_value", [
    ("allow_secret_access", "false"),
    ("allow_secret_access", 0),
    ("allow_network_write", "false"),
    ("allow_network_write", []),
])
def test_d5310_process_environment_policy_blocks_before_process_start(tmp_path, key, bad_value):
    config, db_path, manifest_path, approval_id = create_probe_fixture(tmp_path)
    config["policy"][key] = bad_value

    result = run_supervised_sandbox_probe(...)

    assert result.status == "sandbox_process_blocked"
    assert "config_contract_invalid" in str(result.error)
    assert no_agent_runs(db_path)
```

**Expected RED:** current code computes environment capability booleans through truthiness after the `allow_real_process_execution` gate.

### Task 6: GREEN implementation in `real_adapter_executor.py`

**Objective:** Replace target truthiness with literal-bool validation and prevent malformed handoff policy from calling handoff.

**Guidance:**

- Import `validate_policy_bool_schema`.
- Add local constants for environment policy keys.
- Treat `adapter_output_handoff` as optional absent-compatible unless CodeX widens config scope.
- Add `config_contract_errors` metadata to stdout/report/event payloads.
- Ensure `handoff_allowed` is computed from `value is True`, never `bool(value)`.
- Ensure malformed handoff policy does not call `apply_adapter_output_handoff(...)`.

### Task 7: GREEN implementation in `real_adapter_sandbox.py`

**Objective:** Block malformed policy shape / secret-access values before a sandbox plan is marked ready.

**Guidance:**

- Import `validate_policy_bool_schema`.
- Validate `policy` mapping before `_bounded_timeout_seconds(...)`.
- Validate `allow_secret_access` before `_environment_contract(...)`.
- For schema invalid results, record `real_adapter_sandbox_invocation_blocked` with `reason="config_contract_invalid: ..."`, `started_real_process=false`, `execution_authorized=false`, and no agent run.
- Use `schema.values["allow_secret_access"] is True`, not `bool(...)`.

### Task 8: GREEN implementation in `real_adapter_process.py`

**Objective:** Block malformed process/env policy values before process start and environment injection.

**Guidance:**

- Import `validate_policy_bool_schema`.
- Validate required keys before the `allow_real_process_execution` decision and before `_environment_contract(...)` is used.
- Use `schema.values["allow_real_process_execution"] is not True` for the existing false block.
- Use `schema.values["allow_secret_access"] is True` and `schema.values["allow_network_write"] is True` in environment metadata.
- Ensure malformed schema returns `_blocked(..., reason="config_contract_invalid: ...")` with `started_real_process=false` and no `agent_runs` insert.

### Task 9: Targeted regression after GREEN

Run after future implementation is authorized and complete:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/aiwg/runners/test_d5310_real_adapter_policy_consumer_contract_guard.py \
  tests/aiwg/runners/test_b8_real_adapter_dry_run_executor.py \
  tests/aiwg/runners/test_b10_sandbox_invocation_readiness.py \
  tests/aiwg/runners/test_b11_supervised_sandbox_process_harness.py \
  tests/aiwg/test_d536_runtime_policy_contract_guard.py \
  tests/aiwg/test_d537_dispatch_policy_consumer_contract_guard.py \
  -p no:cacheprovider
```

Expected after GREEN:

```text
all selected tests passed
```

Note: the B11 regression starts only harmless Python probe subprocesses inside pytest temp directories and still blocks real agent binaries. Do not run real adapter binaries.

### Task 10: Full safety verification after GREEN

Run after future implementation is authorized and targeted regression passes:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli doctor --config aiwg.yaml
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.mcp.server --config aiwg.yaml --list-tools
```

Expected safety surface:

```text
Full AIWG suite passes
AIWG doctor: OK
MCP tools remain status/list_tasks/get_task/recent_events only
AIVideoTrans marker scan remains 0 hits
```

## Future implementation acceptance artifact

Future implementation, if explicitly authorized, should write:

```text
docs/ai-workgroup/state/artifacts/phase-d5-3-10-real-adapter-policy-consumer-strict-config-alignment/acceptance.json
```

It must remain:

```text
status = completed_ready_for_codex_review
codex_review.status = pending
codex_review.passed = null
```

until the user provides CodeX review results.

Acceptance evidence should include:

- RED failures before implementation;
- GREEN targeted test pass;
- targeted regression pass;
- full `tests/aiwg` pass;
- doctor OK;
- MCP read-only surface;
- protected business repository marker scan = 0 hits;
- static scan confirming target `bool(policy.get(...))` / `bool((config.get("policy") or {}).get(...))` consumers are removed from the three real-adapter files or intentionally classified as non-target payload/readiness bools.

## CodeX review focus for this planning artifact

Ask CodeX to confirm:

1. D5.3.10 should target exactly `real_adapter_process.py`, `real_adapter_executor.py`, and `real_adapter_sandbox.py` first.
2. `adapter_output_handoff` should be absent-compatible in this slice because checked-in `aiwg.yaml` currently omits it, unless CodeX wants explicit `aiwg.yaml` safe-default alignment.
3. `default_timeout_minutes` numeric schema in `real_adapter_sandbox._bounded_timeout_seconds()` should stay out of scope except for policy mapping fail-closed ordering.
4. Returning `config_contract_invalid` metadata without broad executor dataclass churn is acceptable for `execute_real_adapter_dry_run(...)`, as long as malformed `adapter_output_handoff` never calls handoff.
5. Future implementation must wait for explicit authorization after planning review passes.

## Non-goals

D5.3.10 is not a repo-wide policy migration. Leave these for later slices unless CodeX explicitly re-scopes:

```text
aiwg/doctor.py _as_bool messaging paths
aiwg/runners/orchestrator.py bool(policy.get(key)) helper
aiwg/operator_approval.py real_adapter_execution_mode string schema
aiwg/adapter_output.py adapter_result.handoff_allowed parsing
numeric policy schema for default_timeout_minutes/default_max_attempts/etc.
MCP mutation tools
real process launch beyond existing harmless pytest probes
protected business repository writes
GitHub write/PR mutation
CodeX Automation modification
```

## Safety boundary for planning step

This planning step only creates the plan and planning acceptance artifact. It performs safe static/read-only verification only.

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
implementation_started = false
```

## Recommended next

Submit this D5.3.10 planning-only artifact to CodeX review. If CodeX passes, wait for explicit Human/CodeX authorization before starting D5.3.10 implementation. Do not infer implementation authorization from planning review pass.
