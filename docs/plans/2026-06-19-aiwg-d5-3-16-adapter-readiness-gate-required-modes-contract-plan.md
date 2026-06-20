# AIWG D5.3.16 Adapter Readiness Gate `required_modes` Contract Plan

> **For Hermes:** This is a planning-only artifact. Do not implement this plan in the same pass. If implementation is later authorized, load `test-driven-development` and execute strict RED -> GREEN -> REFACTOR with CodeX review gating.

**Goal:** Choose exactly one narrow next consumer/surface after D5.3.15: `adapter_readiness_gate.required_modes` in `aiwg/adapter_readiness_gate.py`.

**Architecture:** Keep D5.3.16 focused on the adapter readiness gate's execution-mode skip boundary. The future implementation should validate the configured `required_modes` list before it can decide `execution_mode_not_required`, so malformed config cannot silently bypass the readiness gate.

**Tech Stack:** Python, pytest, AIWG config contract / doctor, `adapter_readiness_gate`, `operator_approval` preflight/real-start entrypoints.

---

## 0. Current gate

CodeX accepted D5.3.15 post-merge reconciliation:

```text
D5.3.15 = fully reconciled after merge
GitHub PR #2 = merged
main = synced with origin/main
working tree = clean
acceptance artifact = updated locally and ignored
MCP surface = read-only only
real agents / writes / deploy / CodeX automation = still disabled
D5.3.16 implementation = not started
```

Current local repository state at planning start:

```text
branch = main
HEAD = 30870f75c036e2e2e2ac939e0ce419d58ac55f89
origin/main = 30870f75c036e2e2e2ac939e0ce419d58ac55f89
ahead = 0
behind = 0
working tree = clean
```

D5.3.16 is authorized only as planning-only:

```text
No implementation
No RED tests
No runtime code changes
No test-file changes
No config changes
No MCP mutation tools
No real agents or writes
No GitHub mutation
No deployment
No CodeX Automation changes
```

## 1. Selected surface: exactly one narrow consumer

D5.3.16 selects exactly one surface:

```text
adapter_readiness_gate.required_modes
```

Current consumer path:

```text
aiwg/adapter_readiness_gate.py:122  required_modes = _required_modes(gate_config)
aiwg/adapter_readiness_gate.py:123  if execution_mode not in required_modes:
aiwg/adapter_readiness_gate.py:130      skipped_reason = "execution_mode_not_required"
aiwg/adapter_readiness_gate.py:383  def _required_modes(gate_config)
aiwg/adapter_readiness_gate.py:384      raw = gate_config.get("required_modes", DEFAULT_REQUIRED_MODES)
aiwg/adapter_readiness_gate.py:385      if not isinstance(raw, list): return DEFAULT_REQUIRED_MODES
aiwg/adapter_readiness_gate.py:387      modes = [str(item) for item in raw if str(item)]
aiwg/adapter_readiness_gate.py:388      return modes or DEFAULT_REQUIRED_MODES
```

Default config source:

```text
aiwg/config.py:162  adapter_readiness_gate:
aiwg/config.py:163    enabled: True
aiwg/config.py:164    max_age_minutes: 60
aiwg/config.py:165    required_modes: ["sandbox_plan", "sandbox_probe", "real"]
```

Current checked-in `aiwg.yaml` omits `adapter_readiness_gate`, so missing section/key must remain absent-compatible and use the default required modes.

## 2. Why this is the right next slice

D5.3.14 hardened `adapter_readiness_gate.enabled` so malformed falsey values cannot disable the gate.

D5.3.15 hardened persisted readiness report interpretation so malformed report content cannot be mistaken for adapter-missing or pass through truthiness.

The next gate-local decision before any readiness report is consulted is the execution-mode skip branch:

```python
required_modes = _required_modes(gate_config)
if execution_mode not in required_modes:
    return AdapterReadinessGateResult(
        allowed=True,
        payload={
            "gate_enabled": True,
            "execution_mode": execution_mode,
            "required_modes": required_modes,
            "skipped_reason": "execution_mode_not_required",
        },
    )
```

This branch is intentionally allowed for valid configuration, for example `required_modes=["real"]` can skip the sandbox planning gate while still requiring the real-start gate.

But malformed `required_modes` values are currently normalized silently. That makes a config contract problem indistinguishable from an intentional skip decision.

## 3. Current risk surface

### 3.1 Non-list values are silently defaulted

Current behavior:

```python
raw = gate_config.get("required_modes", DEFAULT_REQUIRED_MODES)
if not isinstance(raw, list):
    return list(DEFAULT_REQUIRED_MODES)
```

Examples:

```text
required_modes = "real"
required_modes = 0
required_modes = null
required_modes = {"real": true}
```

These should not be accepted as valid present values. Missing section/key can default, but present malformed shape should fail closed as `config_contract_invalid`.

### 3.2 List entries are stringified

Current behavior:

```python
modes = [str(item) for item in raw if str(item)]
```

Examples:

```text
required_modes = [123]
required_modes = [false]
required_modes = [null]
required_modes = [{"mode": "real"}]
```

These can become arbitrary strings and participate in the skip decision. For example, `required_modes=[123]` becomes `["123"]`, so `execution_mode="real"` is not required and the gate can skip before checking readiness.

### 3.3 Unknown strings can bypass all normal modes

Current behavior accepts any non-empty string.

Examples:

```text
required_modes = ["not_a_real_mode"]
required_modes = ["sandbox-plan"]
required_modes = ["REAL"]
```

For normal execution modes (`sandbox_plan`, `sandbox_probe`, `real`), those values can make the gate skip unexpectedly.

### 3.4 Empty lists are ambiguous

Current behavior turns an empty list into defaults:

```python
return modes or list(DEFAULT_REQUIRED_MODES)
```

For safety and reviewability, a present empty list should be considered malformed rather than silently defaulted. Missing key is the only absent-compatible default path.

## 4. D5.3.16 planning-only boundary

Allowed writes in this planning-only phase:

```text
docs/plans/2026-06-19-aiwg-d5-3-16-adapter-readiness-gate-required-modes-contract-plan.md
docs/ai-workgroup/state/artifacts/phase-d5-3-16-adapter-readiness-gate-required-modes-contract-planning/acceptance.json
```

Forbidden during this planning-only phase:

- implementation;
- RED tests or test-file changes;
- runtime code changes under `aiwg/`;
- `aiwg.yaml` changes;
- config contract code changes;
- readiness report writer changes;
- `_max_age_minutes(...)` cleanup;
- Codex `desktop_automation_allowed` cleanup;
- dashboard/status stale-age cleanup;
- MCP mutation tools;
- real agents or external agents;
- real adapter process execution;
- protected AIVideoTrans business repository writes;
- GitHub write APIs, PR comments, PR mutation, `git push`, `git merge`;
- deployment;
- CodeX Automation modification;
- local stale-branch cleanup such as deleting `d5-strict-config-gates [gone]`.

## 5. Planned behavior contract

### 5.1 Absent-compatible default

These cases remain valid and use the default modes:

```text
adapter_readiness_gate section absent
adapter_readiness_gate.required_modes key absent
```

Expected normalized value:

```text
["sandbox_plan", "sandbox_probe", "real"]
```

Rationale: checked-in `aiwg.yaml` currently omits `adapter_readiness_gate`, and absence is an established compatible default.

### 5.2 Valid present values

A present `required_modes` value must be a non-empty list of literal strings drawn from the allowed set:

```text
sandbox_plan
sandbox_probe
real
```

Valid examples:

```text
["sandbox_plan", "sandbox_probe", "real"]
["real"]
["sandbox_probe", "real"]
["sandbox_plan"]
```

The future implementation should preserve valid subset behavior. For example:

```text
required_modes = ["real"]
execution_mode = "sandbox_plan"
allowed = true
payload.skipped_reason = "execution_mode_not_required"
```

and:

```text
required_modes = ["real"]
execution_mode = "real"
```

continues into the normal readiness-gate checks.

### 5.3 Malformed present values fail closed

Present malformed values must not be silently coerced/defaulted/stringified. They should return `config_contract_invalid` before the execution-mode skip branch can allow the gate.

Malformed value classes:

```text
required_modes is not a list
required_modes is an empty list
required_modes contains a non-string item
required_modes contains an empty string
required_modes contains a string outside {sandbox_plan, sandbox_probe, real}
```

Recommended error strings:

```text
config_contract_invalid: adapter_readiness_gate.required_modes must be a non-empty list of literal mode strings
config_contract_invalid: adapter_readiness_gate.required_modes[0] must be a literal string; got int
config_contract_invalid: adapter_readiness_gate.required_modes[0] must be one of ['sandbox_plan', 'sandbox_probe', 'real']; got 'not_a_real_mode'
```

### 5.4 Direct gate blocked-result contract

For direct `evaluate_adapter_readiness_gate(...)` with malformed present `required_modes`:

```text
allowed = false
reason = config_contract_invalid
payload.reason = config_contract_invalid
payload.error = config_contract_invalid
payload.errors contains the exact schema error
payload.execution_mode = <requested mode>
payload.started_real_process = false
payload.started_adapter_process = false
```

The blocked result should be returned before:

```text
execution_mode_not_required skip
latest readiness event lookup
readiness report read
current adapter refresh
Codex lock check
```

### 5.5 `resume_preflight(...)` surface contract

For malformed present `required_modes` reaching `resume_preflight(...)`:

```text
result.status = adapter_readiness_blocked
result.error = config_contract_invalid
latest adapter_readiness_gate_blocked payload.reason = config_contract_invalid
agent_runs count does not increase
operator approvals are not consumed
started_real_process = false
started_adapter_process = false
```

### 5.6 `approve_real_start(...)` surface contract

For malformed present `required_modes` reaching `approve_real_start(...)`:

```text
result.status = adapter_readiness_blocked
result.error = config_contract_invalid
authorization_path is None
agent_runs count does not increase
latest adapter_readiness_gate_blocked payload.reason = config_contract_invalid
started_real_process = false
started_adapter_process = false
```

### 5.7 Doctor / config contract

`python -m aiwg.cli doctor --config <bad-config>` should fail closed with the same `config_contract_invalid` reason for present malformed `adapter_readiness_gate.required_modes` values.

For valid absent/missing-compatible config, doctor should remain OK and may report a new success line such as:

```text
adapter_readiness_gate required_modes schema ok
```

## 6. Future implementation scope after CodeX/Human authorization

Do not execute these steps during planning. If D5.3.16 planning passes CodeX review and the user explicitly authorizes implementation, keep the implementation boundary narrow.

Future allowed files:

```text
aiwg/config.py
aiwg/adapter_readiness_gate.py
tests/aiwg/runners/test_d5316_adapter_readiness_gate_required_modes_contract.py
```

Future implementation outline:

1. Add a focused D5.3.16 test file first.
2. Prove intended RED failures.
3. Add a schema helper in `aiwg/config.py`, for example `validate_adapter_readiness_gate_required_modes_schema(...)`.
4. Wire that helper into `validate_config_contract(...)` so doctor can fail closed.
5. Call the helper in `evaluate_adapter_readiness_gate(...)` before `_required_modes(...)` and before the skip branch.
6. Replace or narrow `_required_modes(...)` so it consumes already-normalized values rather than coercing arbitrary input.
7. Preserve absent section/key defaults.
8. Preserve valid subset skip behavior.
9. Keep `_max_age_minutes(...)`, `_validate_codex_lock(...)`, and report writer/dashboard changes out of scope.
10. Write implementation acceptance as `completed_ready_for_codex_review` with CodeX pending.

## 7. Planned future RED tests, not to be added now

Future test file:

```text
tests/aiwg/runners/test_d5316_adapter_readiness_gate_required_modes_contract.py
```

### Test 1 — absent section defaults to all required modes

Setup config with no `adapter_readiness_gate` key.

Expected:

```text
validate_config_contract(config).ok is true
validate_adapter_readiness_gate_required_modes_schema(config).values["required_modes"] == ["sandbox_plan", "sandbox_probe", "real"]
```

### Test 2 — missing key defaults to all required modes

Setup:

```python
config["adapter_readiness_gate"] = {"enabled": True, "max_age_minutes": 60}
```

Expected default modes as above.

### Test 3 — valid subset preserves execution-mode skip

Setup:

```python
config["adapter_readiness_gate"]["required_modes"] = ["real"]
execution_mode = "sandbox_plan"
```

Expected direct gate result:

```text
allowed = true
payload.skipped_reason = execution_mode_not_required
payload.required_modes = ["real"]
```

This preserves intentional configuration behavior.

### Test 4 — valid subset requires real mode

Setup:

```python
config["adapter_readiness_gate"]["required_modes"] = ["real"]
execution_mode = "real"
```

Expected: the gate does not skip due to `required_modes` and continues to the normal readiness-report precondition path.

A minimal assertion can use no readiness event and expect:

```text
allowed = false
reason = adapter_readiness_report_missing
payload.required_modes = ["real"]
```

### Test 5 — non-list present values fail closed

Parameterized malformed values:

```text
"real"
0
1
null
{"mode": "real"}
true
false
```

Expected direct gate result for each:

```text
allowed = false
reason = config_contract_invalid
payload.error = config_contract_invalid
errors mention adapter_readiness_gate.required_modes
started_real_process = false
started_adapter_process = false
```

### Test 6 — empty list fails closed

Setup:

```python
config["adapter_readiness_gate"]["required_modes"] = []
```

Expected:

```text
reason = config_contract_invalid
errors mention required_modes must be non-empty
```

### Test 7 — non-string items fail closed

Parameterized malformed list items:

```text
[123]
[false]
[null]
[{"mode": "real"}]
[[]]
```

Expected: `config_contract_invalid`, not `execution_mode_not_required`.

### Test 8 — unknown strings fail closed

Parameterized malformed strings:

```text
["not_a_real_mode"]
["sandbox-plan"]
["REAL"]
[""]
```

Expected: `config_contract_invalid`, not `execution_mode_not_required`.

### Test 9 — resume_preflight surfaces required_modes config invalid

Use malformed present `required_modes=[123]` through `resume_preflight(...)`.

Expected:

```text
result.status = adapter_readiness_blocked
result.error = config_contract_invalid
latest adapter_readiness_gate_blocked payload.reason = config_contract_invalid
agent_runs count does not increase
operator approval is not consumed
```

### Test 10 — approve_real_start surfaces required_modes config invalid

Use malformed present `required_modes=[123]` through `approve_real_start(...)`.

Expected:

```text
result.status = adapter_readiness_blocked
result.error = config_contract_invalid
authorization_path is None
agent_runs count does not increase
latest adapter_readiness_gate_blocked payload.started_real_process = false
latest payload.started_adapter_process = false
```

### Test 11 — doctor fails on malformed required_modes

Write a temporary config with:

```yaml
adapter_readiness_gate:
  enabled: true
  max_age_minutes: 60
  required_modes:
    - 123
```

Expected CLI doctor:

```text
exit_code != 0
stdout/stderr contains config_contract_invalid
stdout/stderr contains adapter_readiness_gate.required_modes
```

### Test 12 — doctor remains OK on absent adapter_readiness_gate

Use current checked-in `aiwg.yaml` or an equivalent config with no `adapter_readiness_gate` section.

Expected:

```text
exit_code = 0
AIWG doctor: OK
```

## 8. Non-goals for D5.3.16

D5.3.16 must not include:

- implementation during planning;
- RED tests during planning;
- changes to runtime/test/config files during planning;
- `adapter_readiness_gate.enabled` changes already covered by D5.3.14;
- readiness report interpretation changes already covered by D5.3.15;
- `_max_age_minutes(...)` validation;
- dashboard `_adapter_readiness_max_age_minutes(...)` validation;
- Codex `desktop_automation_allowed` bool normalization;
- report writer schema changes in `aiwg/adapter_binary_readiness.py`;
- `aiwg.yaml` changes;
- real process execution;
- MCP mutation tools;
- protected business repository writes;
- GitHub PR mutation/comments;
- CodeX Automation changes;
- stale local branch cleanup.

## 9. Planning acceptance criteria

D5.3.16 planning-only is ready for CodeX review when:

1. This plan exists under `docs/plans/`.
2. Planning acceptance exists under `docs/ai-workgroup/state/artifacts/phase-d5-3-16-adapter-readiness-gate-required-modes-contract-planning/acceptance.json`.
3. Acceptance status is `completed_ready_for_codex_review`.
4. `planning_only=true`.
5. `implementation_started=false`.
6. `red_tests_added=false`.
7. `runtime_code_changed=false`.
8. `test_code_changed=false`.
9. `config_changed=false`.
10. `codex_review.status=pending` and `codex_review.passed=null`.
11. The plan chooses exactly one surface: `adapter_readiness_gate.required_modes`.
12. `doctor` remains OK.
13. MCP `--list-tools` remains read-only only: `status`, `list_tasks`, `get_task`, `recent_events`.
14. Protected AIVideoTrans marker scan for D5.3.16 terms has `0 hits`.
15. Git diff contains only this planning doc as a tracked repository change; local acceptance remains ignored state.
16. No runtime/test/config/business-repo files are changed.

## 10. CodeX quick review focus

Ask CodeX to review these choices before implementation:

1. Is `adapter_readiness_gate.required_modes` the right D5.3.16 surface after D5.3.15?
2. Should missing `adapter_readiness_gate` and missing `required_modes` stay absent-compatible with default modes?
3. Should present non-list `required_modes` values fail closed instead of defaulting?
4. Should present empty lists fail closed rather than defaulting?
5. Should every present item be a literal string from `{sandbox_plan, sandbox_probe, real}`?
6. Is preserving valid subset behavior, especially `["real"]`, correct?
7. Should direct gate, `resume_preflight(...)`, `approve_real_start(...)`, and doctor all surface `config_contract_invalid` for malformed present values?
8. Confirm `_max_age_minutes(...)` and Codex `desktop_automation_allowed` cleanup remain separate future slices.
9. Confirm no implementation or RED tests should begin until CodeX/Human authorization after planning review.

## 11. Recommended next

Submit this D5.3.16 planning-only artifact to CodeX review. Stop after planning acceptance is written and read-only verification passes.
