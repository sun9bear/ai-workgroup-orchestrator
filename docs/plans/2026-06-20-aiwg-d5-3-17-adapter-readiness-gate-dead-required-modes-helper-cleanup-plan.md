# AIWG D5.3.17 Adapter Readiness Gate Dead `_required_modes()` Helper Cleanup Plan

> **For Hermes:** This is a planning-only artifact. Do not implement this plan in the same pass. Do not add RED tests, do not edit runtime code, do not edit `aiwg.yaml`, do not enable MCP mutation tools, do not use GitHub mutation, do not touch the protected AIVideoTrans business repository, and do not change CodeX Automation.

**Goal:** Plan the smallest post-D5.3.16 cleanup slice: remove the now-unused lenient `_required_modes()` helper from `aiwg/adapter_readiness_gate.py` after D5.3.16 moved `required_modes` semantics into strict config validation.

**Architecture:** D5.3.16 made `evaluate_adapter_readiness_gate(...)` consume `validate_adapter_readiness_gate_required_modes_schema(config)` directly, so `required_modes` defaults and validation now live in `aiwg/config.py`. The old `_required_modes(gate_config)` helper still exists in `aiwg/adapter_readiness_gate.py`, but it is no longer called and still encodes the pre-D5.3.16 lenient coercion/defaulting behavior. D5.3.17 should remove that dead helper and its now-unused local default constant/import without changing runtime behavior.

**Tech Stack:** Python, pytest, AIWG fake/dry-run/preflight safety model, Git ignored acceptance artifacts under `docs/ai-workgroup/state/`.

---

## 1. Planning-only boundary

This D5.3.17 step is planning-only.

Allowed writes in this planning step:

- `docs/plans/2026-06-20-aiwg-d5-3-17-adapter-readiness-gate-dead-required-modes-helper-cleanup-plan.md`
- `docs/ai-workgroup/state/artifacts/phase-d5-3-17-adapter-readiness-gate-dead-required-modes-helper-cleanup-planning/acceptance.json`

Explicitly forbidden in this planning step:

- no runtime code edits;
- no test edits;
- no RED tests;
- no `aiwg.yaml` edits;
- no MCP mutation tools;
- no GitHub mutation;
- no commit or push;
- no protected business repository writes;
- no deployment;
- no CodeX Automation changes;
- no D5.3.17 implementation before CodeX/Human review accepts this plan.

This plan must stop at:

```text
completed_ready_for_codex_review
```

---

## 2. Selected surface

Exactly one selected surface:

```text
adapter_readiness_gate._required_modes dead helper cleanup
```

Chosen over the deferred alternatives:

- `codex.desktop_automation_allowed` / Codex desktop automation bool cleanup is still a real behavior/schema hardening slice and should remain separate.
- `adapter_readiness_gate.max_age_minutes` still affects stale-report behavior and requires a contract decision about integer parsing/defaults; it is broader than a post-merge cleanup.
- The dead `_required_modes()` helper is the smallest cleanup after D5.3.16 because it removes unreachable lenient parser code without changing execution semantics.

---

## 3. Evidence from current `main`

Current repository state at planning start:

```text
branch = main
HEAD = d9108fc68df4e8948e6ba27bc7ac26c9d70ffdc2
origin/main = d9108fc68df4e8948e6ba27bc7ac26c9d70ffdc2
ahead = 0
behind = 0
working tree = clean
```

D5.3.16 post-merge acceptance states:

```text
D5.3.16 status = codex_review_passed
PR #3 = merged
merge commit = d9108fc68df4e8948e6ba27bc7ac26c9d70ffdc2
post_merge_reconciliation.status = completed
```

Relevant current code paths:

```text
aiwg/adapter_readiness_gate.py:103-138
```

`evaluate_adapter_readiness_gate(...)` currently validates and reads `required_modes` through:

```python
gate_schema = validate_adapter_readiness_gate_bool_schema(config)
required_modes_schema = validate_adapter_readiness_gate_required_modes_schema(config)
required_modes = required_modes_schema.values["required_modes"]
```

The old helper remains at:

```text
aiwg/adapter_readiness_gate.py:404-409
```

Current dead helper body:

```python
def _required_modes(gate_config: dict[str, Any]) -> list[str]:
    raw = gate_config.get("required_modes", DEFAULT_REQUIRED_MODES)
    if not isinstance(raw, list):
        return list(DEFAULT_REQUIRED_MODES)
    modes = [str(item) for item in raw if str(item)]
    return modes or list(DEFAULT_REQUIRED_MODES)
```

This helper is stale because it still stringifies items and silently defaults malformed present values. D5.3.16 deliberately replaced that behavior with fail-closed strict validation:

```text
present malformed required_modes -> config_contract_invalid
validation happens before execution_mode_not_required skip
```

Search evidence at planning time:

```text
_required_modes( call sites in runtime = only the helper definition
DEFAULT_REQUIRED_MODES usages = local constant + dead helper only
```

Therefore future implementation can remove:

- `ADAPTER_READINESS_GATE_REQUIRED_MODES_DEFAULT` import from `aiwg/adapter_readiness_gate.py`, if unused after deletion;
- `DEFAULT_REQUIRED_MODES = list(ADAPTER_READINESS_GATE_REQUIRED_MODES_DEFAULT)`, if unused after deletion;
- `_required_modes(...)` helper.

Do not remove or change:

- `DEFAULT_MAX_AGE_MINUTES`;
- `_max_age_minutes(...)`;
- `validate_adapter_readiness_gate_required_modes_schema(...)` in `aiwg/config.py`;
- D5.3.16 tests;
- runtime behavior around `execution_mode_not_required`.

---

## 4. Planned behavior contract

D5.3.17 is intended to be a cleanup-only implementation. It must preserve all D5.3.16 externally visible behavior.

Must remain true after future implementation:

1. Absent `adapter_readiness_gate` remains accepted by config validation.
2. Missing `adapter_readiness_gate.required_modes` defaults to:

   ```python
   ["sandbox_plan", "sandbox_probe", "real"]
   ```

3. Present valid subset such as:

   ```python
   ["real"]
   ```

   still preserves the `execution_mode_not_required` skip for sandbox modes.

4. Present malformed values still fail closed with:

   ```text
   config_contract_invalid
   ```

5. Malformed present values still fail before `execution_mode_not_required` can allow a skip.
6. Direct gate, `resume_preflight(...)`, `approve_real_start(...)`, `doctor`, and CLI doctor behavior remain covered by existing D5.3.16 tests.
7. No real agents, writes, adapter subprocesses, MCP mutation tools, GitHub mutation, protected repo writes, deployment, or CodeX Automation changes are enabled.

This slice intentionally does **not** define new schema behavior.

---

## 5. Future implementation scope after review

Only after CodeX/Human review accepts this planning artifact, future D5.3.17 implementation may touch:

```text
aiwg/adapter_readiness_gate.py
docs/ai-workgroup/state/artifacts/phase-d5-3-17-adapter-readiness-gate-dead-required-modes-helper-cleanup/acceptance.json
```

Future implementation should not touch:

```text
aiwg/config.py
aiwg.yaml
MCP configuration or tool registration
GitHub configuration or PR automation
CodeX Automation files
AIVideoTrans business repository
tests/aiwg/runners/test_d5316_adapter_readiness_gate_required_modes_contract.py
```

Rationale for no new tests in this cleanup slice:

- D5.3.16 already introduced the behavior tests for the `required_modes` contract.
- The planned change removes unreachable code rather than adding behavior.
- The correct verification is baseline/after existing regression, not a new RED test that would encode no new contract.

If CodeX requires a test change later, the implementation should be re-scoped before editing tests.

---

## 6. Future implementation steps

### Task 1: Baseline existing behavior before cleanup

**Objective:** Prove current D5.3.16 behavior is green before removing dead code.

**Files:** none.

**Commands:**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/aiwg/runners/test_d5316_adapter_readiness_gate_required_modes_contract.py -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli doctor --config aiwg.yaml
```

**Expected:**

```text
D5.3.16 targeted tests pass
AIWG doctor: OK
```

### Task 2: Remove the dead helper and unused local default

**Objective:** Delete stale lenient `required_modes` parser code without changing behavior.

**Modify:** `aiwg/adapter_readiness_gate.py`

Remove the helper:

```python
def _required_modes(gate_config: dict[str, Any]) -> list[str]:
    raw = gate_config.get("required_modes", DEFAULT_REQUIRED_MODES)
    if not isinstance(raw, list):
        return list(DEFAULT_REQUIRED_MODES)
    modes = [str(item) for item in raw if str(item)]
    return modes or list(DEFAULT_REQUIRED_MODES)
```

Then remove these if they become unused:

```python
ADAPTER_READINESS_GATE_REQUIRED_MODES_DEFAULT
DEFAULT_REQUIRED_MODES
```

Do not edit `_max_age_minutes(...)` or the Codex desktop automation lock logic.

### Task 3: Verify no stale helper references remain

**Objective:** Ensure the cleanup removed only the unreachable helper surface.

**Commands:**

```bash
PYTHONDONTWRITEBYTECODE=1 python - <<'PY'
from pathlib import Path
text = Path('aiwg/adapter_readiness_gate.py').read_text(encoding='utf-8')
assert 'def _required_modes(' not in text
assert 'DEFAULT_REQUIRED_MODES' not in text
assert 'ADAPTER_READINESS_GATE_REQUIRED_MODES_DEFAULT' not in text
assert 'validate_adapter_readiness_gate_required_modes_schema' in text
print('dead_required_modes_helper_removed=true')
PY
```

**Expected:**

```text
dead_required_modes_helper_removed=true
```

### Task 4: Run existing targeted and adjacent regressions

**Objective:** Prove no behavior changed.

**Commands:**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/aiwg/runners/test_d5316_adapter_readiness_gate_required_modes_contract.py -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/aiwg/runners/test_b13_adapter_readiness_gate_binding.py \
  tests/aiwg/runners/test_d5313_adapter_readiness_gate_config_error_structured_block.py \
  tests/aiwg/runners/test_d5314_adapter_readiness_gate_enabled_contract_guard.py \
  tests/aiwg/runners/test_d5315_adapter_readiness_gate_report_interpretation_schema_normalization.py \
  tests/aiwg/runners/test_d5316_adapter_readiness_gate_required_modes_contract.py \
  -q -p no:cacheprovider
```

**Expected:**

```text
targeted D5.3.16 tests pass
adjacent regression passes
```

### Task 5: Run safety verification

**Objective:** Prove cleanup did not alter AIWG safety posture.

**Commands:**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli doctor --config aiwg.yaml
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.mcp.server --config aiwg.yaml --list-tools
git diff --check
```

**Expected:**

```text
AIWG doctor: OK
MCP tools: status/list_tasks/get_task/recent_events only
git diff --check: clean
```

### Task 6: Write implementation acceptance after future implementation

**Objective:** Record cleanup evidence without claiming CodeX review passed.

**Create:**

```text
docs/ai-workgroup/state/artifacts/phase-d5-3-17-adapter-readiness-gate-dead-required-modes-helper-cleanup/acceptance.json
```

Required status:

```text
status = completed_ready_for_codex_review
codex_review.status = pending
codex_review.passed = null
```

---

## 7. Out of scope

D5.3.17 must not include:

- Codex desktop automation bool cleanup;
- `_max_age_minutes(...)` schema hardening;
- `show_warnings` / report-display bool cleanup;
- `aiwg/config.py` schema changes;
- new MCP tools;
- real agent execution;
- protected business repository writes;
- GitHub commit/push/PR creation by Hermes;
- CodeX Automation edits.

Potential follow-up candidates after D5.3.17 review/merge:

1. `adapter_readiness_gate.max_age_minutes` strict integer/range planning.
2. Codex desktop automation lock bool cleanup for `desktop_automation_allowed` in report/manifest.
3. Adapter readiness report display bool cleanup if a concrete consumer remains.

---

## 8. Planning acceptance checklist

For this planning-only phase to be ready for CodeX review:

- [x] exactly one surface selected: `_required_modes()` dead helper cleanup;
- [x] no runtime code edited;
- [x] no tests edited;
- [x] no RED tests added;
- [x] no `aiwg.yaml` edits;
- [x] no MCP/GitHub/CodeX Automation changes;
- [x] no AIVideoTrans business repo writes;
- [x] read-only verification planned and executed for this planning artifact;
- [x] stop at `completed_ready_for_codex_review`.
