# AIWG D5.3.11 Orchestrator Runner Policy Consumer Strict-Config Alignment Plan

> **For Hermes:** This is a planning-only gate. Do not implement D5.3.11 until this planning artifact passes CodeX review **and** Human/CodeX explicitly authorizes implementation. The target is the Orchestrator runner retry/stale policy helper; this planning step must not edit runtime code, tests, `aiwg.yaml`, MCP tools, CodeX Automations, Git/GitHub state, or the protected AIVideoTrans business repository.

**Goal:** Plan a narrow D5.3.11 hardening slice so `aiwg.runners.orchestrator` stops making stale-claim and retry scheduling decisions from Python truthiness or malformed `policy` shapes.

**Architecture:** Reuse the literal-bool discipline established in D5.3.6-D5.3.10, but keep this slice local to the Orchestrator runner policy helper. The future implementation should validate an absent-compatible optional policy-bool contract for the three runner policy keys, compute typed defaults once, and make `run_once(...)` fail closed with `config_contract_invalid` before stale recovery, retry scheduling, inbox import, claim, adapter dispatch, or verification work can proceed under malformed policy. Because checked-in `aiwg.yaml` does not currently include these three runner policy keys, missing keys must preserve the current safe defaults in this slice unless CodeX explicitly widens scope to config alignment.

**Tech Stack:** Python 3.11, pytest, SQLite control-plane state, existing AIWG `run_once` Orchestrator runner, existing `validate_policy_bool_schema` semantics, B4 failure retry/stale-claim tests, acceptance artifacts under `docs/ai-workgroup/state/artifacts/`.

---

## Planning status

```text
phase = D5.3.11-orchestrator-runner-policy-consumer-strict-config-alignment-planning
status = completed_ready_for_codex_review
implementation_status = not_started
```

This document follows D5.3.10 implementation CodeX review passing. It intentionally stops at planning and does not authorize implementation.

## Upstream gate

D5.3.10 implementation acceptance is reviewed:

```text
docs/ai-workgroup/state/artifacts/phase-d5-3-10-real-adapter-policy-consumer-strict-config-alignment/acceptance.json
status = codex_review_passed
codex_review.status = passed
codex_review.passed = true
```

D5.3.10 recommended next step was:

```text
Proceed to D5.3.11 planning-only. Do not start implementation directly; continue to keep fake/dry-run/preflight-only safety boundaries unless explicitly re-authorized.
```

## Why this consumer is selected for D5.3.11

D5.3.6 aligned the central runtime policy entrypoint. D5.3.7 aligned real-adapter dispatch checks. D5.3.8 aligned Git Steward mutation policy checks. D5.3.9 aligned D5 preflight `_policy_denials()`. D5.3.10 aligned direct real-adapter process/executor/sandbox consumers.

The next high-value direct policy truthiness consumer is now the Orchestrator runner helper:

```text
aiwg/runners/orchestrator.py:309-313
```

It controls whether the Orchestrator:

1. stops on stale-claim recovery and requires human intervention;
2. auto-retries tasks in `needs_revision`;
3. auto-retries tasks marked `can_write`.

These paths can mutate AIWG-owned SQLite state and event logs even while real agents remain disabled. A malformed policy value such as `policy.auto_retry_write_tasks = "false"` is especially risky: Python truthiness treats the string as `True`, which can bypass the current default `False` safety posture for write-task auto-retry.

Because these are runner-control decisions, D5.3.11 should be planning-only first.

## Current code reconnaissance

### Existing helper and call sites

`aiwg/runners/orchestrator.py` currently contains:

```python
def _policy_bool(config: dict[str, Any], key: str, *, default: bool) -> bool:
    policy = config.get("policy") or {}
    if key not in policy:
        return default
    return bool(policy.get(key))
```

Observed call sites:

```text
aiwg/runners/orchestrator.py:60   _policy_bool(config, "stale_claim_requires_human", default=True)
aiwg/runners/orchestrator.py:393  _policy_bool(config, "auto_retry_needs_revision", default=True)
aiwg/runners/orchestrator.py:413  _policy_bool(config, "auto_retry_write_tasks", default=False)
```

### Live config compatibility note

`build_default_config()` contains these policy keys:

```python
"auto_retry_needs_revision": True,
"auto_retry_write_tasks": False,
"stale_claim_requires_human": True,
```

Checked-in `aiwg.yaml` currently does **not** contain those three keys. D5.3.11 should not modify `aiwg.yaml` during planning. Future implementation should therefore treat these runner policy keys as **optional absent-compatible** in this slice:

```text
missing stale_claim_requires_human -> True
missing auto_retry_needs_revision -> True
missing auto_retry_write_tasks -> False
present value must be literal bool
non-mapping policy must fail closed
```

If CodeX wants a broader config-alignment slice, that should be explicit and separate.

### Risk examples

| Policy shape | Current truthiness risk | Desired D5.3.11 behavior |
| --- | --- | --- |
| `policy.auto_retry_write_tasks = "false"` | `bool("false")` is `True`, so a `can_write` task can auto-retry despite the safe default being `False`. | Return/record `config_contract_invalid`; do not schedule retry. |
| `policy.auto_retry_needs_revision = "false"` | `bool("false")` is `True`, so invalid config still schedules retry. | Return/record `config_contract_invalid`; do not schedule retry. |
| `policy.stale_claim_requires_human = 0` | `bool(0)` is `False`, so a stale-claim recovery gate can be skipped even though `0` is not a literal bool. | Return/record `config_contract_invalid`; do not dispatch the next task behind the stale claim. |
| `policy = ["not", "mapping"]` | Non-mapping policy can crash at `.get(...)` or be silently defaulted if falsey. | Return/record `config_contract_invalid` before runner state mutation. |

## Selected D5.3.11 implementation surface after future authorization

Default future implementation may touch only:

```text
aiwg/runners/orchestrator.py
tests/aiwg/runners/test_d5311_orchestrator_policy_consumer_contract_guard.py
docs/ai-workgroup/state/artifacts/phase-d5-3-11-orchestrator-runner-policy-consumer-strict-config-alignment/acceptance.json
```

Planning-only files created by this step:

```text
docs/plans/2026-06-09-aiwg-d5-3-11-orchestrator-runner-policy-consumer-strict-config-alignment-plan.md
docs/ai-workgroup/state/artifacts/phase-d5-3-11-orchestrator-runner-policy-consumer-strict-config-alignment-planning/acceptance.json
```

Optional only if CodeX explicitly requests scope widening:

```text
aiwg/config.py          # only if an optional-policy-bool helper should be centralized
aiwg.yaml              # only if safe-default config alignment is explicitly requested
tests/aiwg/runners/test_b4_failure_retry_policy.py  # only if CodeX prefers extending existing B4 tests instead of a D5.3.11 file
```

## Explicitly out of scope

Do **not** include in D5.3.11 planning or future implementation unless CodeX/Human explicitly widens scope:

```text
real agents
external agents
MCP mutation tools
protected AIVideoTrans business repository writes
GitHub write / PR mutation / PR comments
git commit / push / merge
deploy
CodeX Automation modification
aiwg.yaml changes
full repo-wide bool(...) migration
aiwg/doctor.py _as_bool messaging cleanup
aiwg/policy.py global_kill_switch path schema
aiwg/scope.py global_kill_switch prefix schema
aiwg/state/database.py default_timeout_minutes numeric schema
aiwg/operator_approval.py TTL / real_adapter_execution_mode schema
aiwg/real_adapter_sandbox.py default_timeout_minutes numeric schema
aiwg/adapter_output.py adapter_result.handoff_allowed parsing
payload/task/frontmatter bool normalization unrelated to policy config
```

## Planned behavior contract

### Runner policy bool contract

The future implementation should introduce a small runner-specific contract, either local to `aiwg/runners/orchestrator.py` or factored only if CodeX asks for centralization:

```python
RUNNER_OPTIONAL_POLICY_BOOL_DEFAULTS = {
    "stale_claim_requires_human": True,
    "auto_retry_needs_revision": True,
    "auto_retry_write_tasks": False,
}
```

Contract:

1. `policy` must be a mapping.
2. Each key in `RUNNER_OPTIONAL_POLICY_BOOL_DEFAULTS` is optional because checked-in `aiwg.yaml` omits them.
3. If a key is absent, use the existing default.
4. If a key is present, `type(value) is bool` is required.
5. Non-literal values (`"false"`, `"true"`, `0`, `1`, `None`, lists, objects) fail closed with `config_contract_invalid`.
6. No `bool(policy.get(...))` coercion remains for these runner policy keys.

### Fail-closed outcome

Recommended implementation behavior:

- validate this runner policy contract at the beginning of `run_once(...)`, before `init_database(...)`, `import_inbox(...)`, `release_stale_claims(...)`, retry scheduling, claim, adapter dispatch, or verification commands;
- on invalid contract, return `RunOnceResult` with:

```text
status = policy_denied
error = config_contract_invalid
policy_reasons contains policy path errors
import_result = ImportResult()
stale_result = StaleClaimResult()
message_id = null
```

This avoids SQLite state mutation from a malformed runner policy. If CodeX prefers a different machine-readable status, it should remain explicit and test-covered, but must not silently coerce or proceed.

## Future implementation tasks after CodeX/Human authorization

### Task 1: RED test for non-mapping policy fail-closed before runner work

**Objective:** Prove `run_once(...)` does not crash or proceed when `config["policy"]` is not a mapping.

**Files:**

- Create: `tests/aiwg/runners/test_d5311_orchestrator_policy_consumer_contract_guard.py`
- Future modify: `aiwg/runners/orchestrator.py`

**Step 1: Write failing test**

```python
from pathlib import Path

from aiwg.config import build_default_config
from aiwg.runners.orchestrator import run_once


def build_test_config(tmp_path: Path) -> dict:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    return config


def test_d5311_run_once_rejects_non_mapping_policy_before_runner_work(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config["policy"] = ["not", "mapping"]

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "policy_denied"
    assert result.error == "config_contract_invalid"
    assert any("policy must be a mapping" in reason for reason in result.policy_reasons)
    assert not (tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite").exists()
```

**Expected RED:** current code initializes/imports first and/or crashes through non-mapping policy access rather than returning a structured `config_contract_invalid` result.

### Task 2: RED test for `auto_retry_write_tasks = "false"`

**Objective:** Prove string truthiness can currently bypass the safe default for write-task auto-retry.

**Files:**

- Test: `tests/aiwg/runners/test_d5311_orchestrator_policy_consumer_contract_guard.py`
- Read helper pattern from: `tests/aiwg/runners/test_b4_failure_retry_policy.py`

**Step 1: Add a needs-revision write-task fixture**

Create or reuse helper logic to:

1. create a task with `can_write: true`, `status = needs_revision`, `attempt < max_attempts`;
2. set `config["policy"]["auto_retry_write_tasks"] = "false"`;
3. call `run_once(...)`.

**Expected future assertion:**

```python
assert result.status == "policy_denied"
assert result.error == "config_contract_invalid"
assert any("policy.auto_retry_write_tasks" in reason for reason in result.policy_reasons)
assert task remains unscheduled / not moved back to ready
```

**Expected RED:** current `_policy_bool(...)` treats `"false"` as `True`, so the write task may be retried instead of blocked.

### Task 3: RED test for `auto_retry_needs_revision = "false"`

**Objective:** Prove malformed auto-retry policy values do not schedule retry for any needs-revision task.

**Test shape:**

```python
def test_d5311_auto_retry_needs_revision_requires_literal_bool(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config["policy"]["auto_retry_needs_revision"] = "false"
    # seed/import a needs_revision task with remaining attempts
    result = run_once(config=config, project_root=tmp_path, agent="Fake")
    assert result.status == "policy_denied"
    assert result.error == "config_contract_invalid"
    assert any("policy.auto_retry_needs_revision" in reason for reason in result.policy_reasons)
```

**Expected RED:** current string truthiness can still allow retry scheduling.

### Task 4: RED test for `stale_claim_requires_human = 0`

**Objective:** Prove non-literal falsey values cannot skip stale recovery and dispatch another ready task.

**Test shape:**

Use the existing B4 stale-claim pattern:

```python
def test_d5311_stale_claim_requires_human_requires_literal_bool(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config["policy"]["stale_claim_requires_human"] = 0
    # create one stale task and one ready task behind it
    result = run_once(config=config, project_root=tmp_path, agent="Fake")
    assert result.status == "policy_denied"
    assert result.error == "config_contract_invalid"
    assert any("policy.stale_claim_requires_human" in reason for reason in result.policy_reasons)
    # next ready task behind stale claim must not be dispatched
```

**Expected RED:** current `bool(0)` skips the stale-claim human-recovery gate.

### Task 5: GREEN runner policy contract helper

**Objective:** Add a local helper that validates only the runner policy keys and preserves absent-compatible defaults.

**Files:**

- Modify: `aiwg/runners/orchestrator.py`

**Implementation guidance:**

```python
@dataclass(frozen=True)
class RunnerPolicyBoolContract:
    ok: bool
    values: dict[str, bool]
    errors: list[str]


RUNNER_OPTIONAL_POLICY_BOOL_DEFAULTS = {
    "stale_claim_requires_human": True,
    "auto_retry_needs_revision": True,
    "auto_retry_write_tasks": False,
}


def _runner_policy_bool_contract(config: dict[str, Any]) -> RunnerPolicyBoolContract:
    policy = config.get("policy")
    if not isinstance(policy, dict):
        return RunnerPolicyBoolContract(
            ok=False,
            values=dict(RUNNER_OPTIONAL_POLICY_BOOL_DEFAULTS),
            errors=["policy schema invalid: policy must be a mapping"],
        )

    values = dict(RUNNER_OPTIONAL_POLICY_BOOL_DEFAULTS)
    errors: list[str] = []
    for key in RUNNER_OPTIONAL_POLICY_BOOL_DEFAULTS:
        if key not in policy:
            continue
        value = policy[key]
        if type(value) is not bool:
            errors.append(f"policy.{key} must be literal bool; got {type(value).__name__}")
            continue
        values[key] = value
    return RunnerPolicyBoolContract(ok=not errors, values=values, errors=errors)
```

Do not use `bool(value)` for policy config values.

### Task 6: GREEN integrate helper into `run_once(...)`

**Objective:** Ensure malformed runner policy fails before state mutation and policy decisions.

**Files:**

- Modify: `aiwg/runners/orchestrator.py`

**Implementation guidance:**

At the top of `run_once(...)`, before `init_database(...)`:

```python
runner_policy = _runner_policy_bool_contract(config)
if not runner_policy.ok:
    return RunOnceResult(
        agent=agent,
        status="policy_denied",
        message_id=None,
        import_result=ImportResult(),
        stale_result=StaleClaimResult(),
        error="config_contract_invalid",
        policy_reasons=["config_contract_invalid: " + error for error in runner_policy.errors],
    )
```

Then replace call sites with typed values:

```python
if runner_policy.values["stale_claim_requires_human"]:
    ...

if not runner_policy.values["auto_retry_needs_revision"]:
    ...

if bool(task.get("can_write")) and not runner_policy.values["auto_retry_write_tasks"]:
    ...
```

Keep `bool(task.get("can_write"))` out of scope: it is task/frontmatter normalization, not `policy` config truthiness.

### Task 7: GREEN static scan and regression

**Objective:** Verify the targeted policy truthiness consumer is removed without expanding scope.

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg/runners/test_d5311_orchestrator_policy_consumer_contract_guard.py -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg/runners/test_d5311_orchestrator_policy_consumer_contract_guard.py tests/aiwg/runners/test_b4_failure_retry_policy.py tests/aiwg/runners/test_a3_fake_run_once.py tests/aiwg/test_d536_runtime_policy_contract_guard.py tests/aiwg/runners/test_d5310_real_adapter_policy_consumer_contract_guard.py -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg -p no:cacheprovider
```

Static scan target after GREEN:

```text
aiwg/runners/orchestrator.py has no bool(policy.get(...)) and no return bool(policy.get(key)) for runner policy decisions.
```

## Acceptance criteria for future implementation

Future D5.3.11 implementation acceptance may be written only after:

1. RED tests fail for the intended runner policy contract gaps.
2. GREEN implementation passes the D5.3.11 targeted test.
3. Targeted regression passes with B4 retry/stale, A3 fake run-once, D5.3.6 runtime policy guard, and D5.3.10 real-adapter policy guard.
4. Full `tests/aiwg` passes.
5. `doctor` remains OK with only known non-git warning if present.
6. MCP `--list-tools` exposes only read-only tools: `status`, `list_tasks`, `get_task`, `recent_events`.
7. Protected AIVideoTrans marker scan for D5.3.11 terms has `0 hits`.
8. Static scan confirms targeted runner policy truthiness consumer is removed.
9. No `aiwg.yaml` change unless CodeX/Human explicitly widens scope.

Implementation acceptance status must be:

```text
status = completed_ready_for_codex_review
codex_review.status = pending
codex_review.passed = null
```

until CodeX review results are provided.

## CodeX review focus for this planning artifact

Ask CodeX to confirm:

1. D5.3.11 should target `aiwg/runners/orchestrator.py` runner policy helper/call sites first.
2. The three runner policy keys should be optional absent-compatible in this slice because checked-in `aiwg.yaml` omits them.
3. `policy.auto_retry_write_tasks = "false"` is the highest-risk RED case because it can invert the safe default for write-task retries through Python truthiness.
4. Returning `RunOnceResult(status="policy_denied", error="config_contract_invalid", policy_reasons=[...])` before `init_database(...)` is acceptable for malformed runner policy.
5. `bool(task.get("can_write"))`, payload bool normalization, and CLI argparse bools stay out of scope.
6. Future implementation must wait for explicit authorization after planning review passes.

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
runtime_files_modified = false
test_files_modified = false
implementation_started = false
```

## Recommended next

Submit this D5.3.11 planning-only artifact to CodeX review. If CodeX passes, wait for explicit Human/CodeX authorization before starting D5.3.11 implementation. Do not infer implementation authorization from planning review pass.
