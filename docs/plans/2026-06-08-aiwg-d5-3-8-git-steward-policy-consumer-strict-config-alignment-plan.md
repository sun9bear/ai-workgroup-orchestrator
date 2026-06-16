# D5.3.8 Git Steward Policy Consumer Strict Config Alignment Plan

> **For Hermes:** This is a planning-only gate. Do not implement D5.3.8 until this planning artifact passes CodeX review. When implementation is later authorized, use `test-driven-development` and keep the slice narrow.

**Goal:** Plan a narrow D5.3.8 hardening slice so Git Steward dry-run mutation-policy consumers reject malformed, missing, non-mapping, or non-literal boolean config instead of relying on Python truthiness.

**Architecture:** Keep Git Steward dry-run-only. Add strict bool schema validation immediately inside `aiwg/git_steward.py` before `_mutation_policy_denials()` interprets `policy.*` and `git.*` mutation switches. Reuse `validate_policy_bool_schema()` for `policy.*`; add only a local Git Steward helper for `git.*` booleans unless CodeX explicitly asks for a broader reusable config helper.

**Tech Stack:** Python 3.11, pytest, SQLite control-plane state, YAML config, existing AIWG CLI. No real Git writes, no GitHub mutation, no MCP mutation tools.

---

## Planning status

```text
phase = D5.3.8-git-steward-policy-consumer-strict-config-alignment-planning
status = completed_ready_for_codex_review
implementation_status = not_started
```

This document follows D5.3.7 CodeX-approved implementation and intentionally stops at planning. It does **not** authorize implementation or any real execution.

## Current findings from code inspection

### Target consumer

`aiwg/git_steward.py` currently uses direct truthiness in `_mutation_policy_denials()`:

```python
def _mutation_policy_denials(config: dict[str, Any]) -> list[str]:
    policy = config.get("policy") or {}
    git = config.get("git") or {}
    denials: list[str] = []
    for key in _POLICY_MUTATION_FLAGS:
        if bool(policy.get(key, False)):
            denials.append(f"{key}=true")
    for key in _GIT_MUTATION_FLAGS:
        if bool(git.get(key, False)):
            denials.append(f"git.{key}=true")
    return denials
```

Observed line references at planning time:

```text
aiwg/git_steward.py:547-557  _mutation_policy_denials direct truthiness
aiwg/git_steward.py:23-36   _POLICY_MUTATION_FLAGS
aiwg/git_steward.py:37-43   _GIT_MUTATION_FLAGS
aiwg/git_steward.py:124-164 plan_git_dry_run policy_denied path
aiwg/cli.py:832-869         git-plan CLI calls plan_git_dry_run; --dry-run remains mandatory
```

Risk points:

- `policy = config.get("policy") or {}` silently treats missing / empty / some malformed policy as an empty mapping.
- `git = config.get("git") or {}` silently treats missing / empty / some malformed git config as an empty mapping.
- `bool(policy.get(...))` and `bool(git.get(...))` interpret strings, integers, lists, and other non-literal values using Python truthiness.
- Empty non-mapping sections such as `policy=[]` or `git=[]` can be silently accepted as `{}` because empty lists are falsey.
- Non-empty non-mapping sections can raise an unstructured `AttributeError` instead of returning a deterministic `policy_denied` result.

### Why this matters even though Git Steward is dry-run-only

The current behavior is often conservative for quoted strings like `"false"` because `bool("false")` is `True`, causing denial. However, D5.3.x is about **strict config contract alignment**, not just accidental safety. Git Steward must not silently accept these malformed shapes:

```text
policy.allow_push = 0
policy.allow_push = "false"
policy.allow_push missing
policy = []
git.allow_auto_commit = 0
git.allow_auto_commit = "false"
git.allow_auto_commit missing
git = []
```

All of those should fail closed as `config_contract_invalid` instead of being accepted, mislabeled as `...=true`, or crashing.

## Scope

### In scope for future D5.3.8 implementation

Future implementation may touch only:

```text
aiwg/git_steward.py
tests/aiwg/git/test_d538_git_steward_policy_consumer_contract_guard.py
docs/ai-workgroup/state/artifacts/phase-d5-3-8-git-steward-policy-consumer-strict-config-alignment/acceptance.json
```

If CodeX explicitly asks for a shared helper, the implementation may instead add a narrowly named generic section-bool helper in `aiwg/config.py`, but the default implementation path should avoid widening the scope beyond Git Steward.

### Out of scope

Do **not** modify or enable:

```text
aiwg.yaml
aiwg/d5_preflight.py
aiwg/adapter_registry.py
aiwg/operator_approval.py
aiwg/doctor.py
aiwg/mcp/*
D:/example/protected-business-repo
CodeX Automations
GitHub PR/comment/write APIs
real agent adapters
real adapter process execution
real Git commit / push / merge / PR creation
```

Do **not** change `git-plan --dry-run` semantics beyond config-contract error classification. It must remain dry-run-only.

## Expected behavior contract

### Schema-valid values

Literal booleans are accepted by schema validation:

```yaml
policy:
  allow_push: false
  allow_merge: false
git:
  enabled: false
  allow_auto_commit: false
```

Then the existing mutation-denial semantics continue:

- literal `False` means no denial for that key;
- literal `True` means the existing denial remains, e.g. `allow_push=true` or `git.allow_auto_commit=true`;
- a literal `True` value does **not** authorize a real Git operation because Git Steward remains dry-run-only and subsequent gates still refuse mutation.

### Schema-invalid values

These must fail closed before normal mutation-denial interpretation:

```text
string values: "false", "true"
numeric values: 0, 1
missing required keys
non-mapping policy or git sections
null / list / object values for bool fields
```

Expected status and reason shape:

```text
result.status = policy_denied
result.denied_reasons includes config_contract_invalid: <section>.<key> ... literal bool ...
target_writes_performed = false
git_commit_performed = false
git_push_performed = false
git_merge_performed = false
mcp_mutation_tools_exposed = false
```

For schema-invalid config, prefer returning only `config_contract_invalid` reasons rather than mixing them with normal `...=true` mutation denials. This makes CodeX review and operator triage deterministic.

## Implementation plan for later authorization

### Task 1: Add RED tests for malformed `policy.*` mutation flags

**Objective:** Prove Git Steward currently accepts or misclassifies malformed policy booleans.

**Files:**

- Create: `tests/aiwg/git/test_d538_git_steward_policy_consumer_contract_guard.py`
- Read-only reference: `tests/aiwg/git/test_d4_git_steward_dry_run.py`

**Test skeleton:**

```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aiwg.config import build_default_config, dump_config
from aiwg.git_steward import plan_git_dry_run
from aiwg.state.database import resolve_db_path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MISSING = object()


def build_test_config(project_root: Path) -> dict[str, Any]:
    config = build_default_config(project_root=project_root)
    config["project_root"] = str(project_root)
    return config


def assert_no_target_git_side_effects(target_root: Path) -> None:
    assert not (target_root / ".codex_worktrees").exists()
    assert not list(target_root.rglob("git-plan-*.json"))
    assert not list(target_root.rglob("pr-gate-*.json"))


def run_plan(config: dict[str, Any], tmp_path: Path):
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    result = plan_git_dry_run(
        config=config,
        project_root=tmp_path,
        plan_id="D538-plan-contract",
        task_id="APF3b-contract",
        target_root=target_root,
        requested_scope="apf_backend",
        changed_files=["src/services/anonymous_preview_admission.py"],
        base_branch="main",
    )
    return result, target_root


@pytest.mark.parametrize(
    ("key", "value", "expected_type"),
    [
        ("allow_push", "false", "str"),
        ("allow_merge", "true", "str"),
        ("allow_write", 0, "int"),
        ("allow_deploy", 1, "int"),
        ("allow_network_write", [], "list"),
    ],
)
def test_d538_policy_mutation_flags_require_literal_bool(
    tmp_path: Path,
    key: str,
    value: Any,
    expected_type: str,
) -> None:
    config = build_test_config(tmp_path)
    config["policy"][key] = value

    result, target_root = run_plan(config, tmp_path)

    assert result.status == "policy_denied"
    assert any("config_contract_invalid" in reason for reason in result.denied_reasons)
    assert any(f"policy.{key}" in reason for reason in result.denied_reasons)
    assert any(expected_type in reason for reason in result.denied_reasons)
    assert result.git_commit_performed is False
    assert result.git_push_performed is False
    assert result.git_merge_performed is False
    assert result.mcp_mutation_tools_exposed is False
    assert_no_target_git_side_effects(target_root)
```

**RED command:**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg/git/test_d538_git_steward_policy_consumer_contract_guard.py -p no:cacheprovider
```

**Expected RED:**

- string values may produce `allow_push=true` rather than `config_contract_invalid`;
- numeric zero / list / missing values may be accepted as planned or crash;
- tests fail before production code changes.

### Task 2: Add RED tests for missing and non-mapping `policy`

**Objective:** Prove missing keys and non-mapping policy fail closed as schema errors.

**Test additions:**

```python
def test_d538_missing_policy_mutation_flag_is_config_contract_invalid(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    del config["policy"]["allow_push"]

    result, target_root = run_plan(config, tmp_path)

    assert result.status == "policy_denied"
    assert any("config_contract_invalid" in reason for reason in result.denied_reasons)
    assert any("policy.allow_push is required and must be literal bool" in reason for reason in result.denied_reasons)
    assert_no_target_git_side_effects(target_root)


@pytest.mark.parametrize("policy_value", [[], ["not", "mapping"], None])
def test_d538_policy_section_must_be_mapping(tmp_path: Path, policy_value: Any) -> None:
    config = build_test_config(tmp_path)
    config["policy"] = policy_value

    result, target_root = run_plan(config, tmp_path)

    assert result.status == "policy_denied"
    assert any("config_contract_invalid" in reason for reason in result.denied_reasons)
    assert any("policy schema invalid" in reason for reason in result.denied_reasons)
    assert_no_target_git_side_effects(target_root)
```

### Task 3: Add RED tests for malformed `git.*` mutation flags

**Objective:** Prove Git Steward applies the same literal-bool contract to `git.enabled` and `git.allow_auto_*`.

**Test additions:**

```python
@pytest.mark.parametrize(
    ("key", "value", "expected_type"),
    [
        ("enabled", "false", "str"),
        ("allow_auto_commit", "false", "str"),
        ("allow_auto_push", 0, "int"),
        ("allow_auto_pr", 1, "int"),
        ("allow_auto_merge", [], "list"),
    ],
)
def test_d538_git_mutation_flags_require_literal_bool(
    tmp_path: Path,
    key: str,
    value: Any,
    expected_type: str,
) -> None:
    config = build_test_config(tmp_path)
    config["git"][key] = value

    result, target_root = run_plan(config, tmp_path)

    assert result.status == "policy_denied"
    assert any("config_contract_invalid" in reason for reason in result.denied_reasons)
    assert any(f"git.{key}" in reason for reason in result.denied_reasons)
    assert any(expected_type in reason for reason in result.denied_reasons)
    assert_no_target_git_side_effects(target_root)


def test_d538_missing_git_mutation_flag_is_config_contract_invalid(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    del config["git"]["allow_auto_pr"]

    result, target_root = run_plan(config, tmp_path)

    assert result.status == "policy_denied"
    assert any("config_contract_invalid" in reason for reason in result.denied_reasons)
    assert any("git.allow_auto_pr is required and must be literal bool" in reason for reason in result.denied_reasons)
    assert_no_target_git_side_effects(target_root)


@pytest.mark.parametrize("git_value", [[], ["not", "mapping"], None])
def test_d538_git_section_must_be_mapping(tmp_path: Path, git_value: Any) -> None:
    config = build_test_config(tmp_path)
    config["git"] = git_value

    result, target_root = run_plan(config, tmp_path)

    assert result.status == "policy_denied"
    assert any("config_contract_invalid" in reason for reason in result.denied_reasons)
    assert any("git schema invalid" in reason for reason in result.denied_reasons)
    assert_no_target_git_side_effects(target_root)
```

### Task 4: Add preservation tests for literal booleans

**Objective:** Ensure D5.3.8 only changes malformed config handling and does not alter dry-run semantics.

**Test additions:**

```python
def test_d538_literal_false_defaults_still_plan_without_git_side_effects(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)

    result, target_root = run_plan(config, tmp_path)

    assert result.status == "planned"
    assert result.dry_run is True
    assert result.target_writes_performed is False
    assert result.git_commit_performed is False
    assert result.git_push_performed is False
    assert result.git_merge_performed is False
    assert result.mcp_mutation_tools_exposed is False
    assert_no_target_git_side_effects(target_root)


def test_d538_literal_true_mutation_flags_keep_existing_denial_reasons(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config["policy"]["allow_push"] = True
    config["policy"]["allow_merge"] = True
    config["git"]["allow_auto_commit"] = True

    result, target_root = run_plan(config, tmp_path)

    assert result.status == "policy_denied"
    assert result.denied_reasons == [
        "allow_push=true",
        "allow_merge=true",
        "git.allow_auto_commit=true",
    ]
    assert result.git_commit_performed is False
    assert result.git_push_performed is False
    assert result.git_merge_performed is False
    assert_no_target_git_side_effects(target_root)
```

### Task 5: Add CLI-level RED test for quoted false YAML

**Objective:** Prove the user-facing `git-plan --dry-run --json` path reports `config_contract_invalid` for quoted YAML booleans.

**Test addition:**

```python
def test_d538_cli_git_plan_reports_config_contract_invalid_for_quoted_false(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config["policy"]["allow_push"] = "false"
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "git-plan",
            "--config",
            str(config_path),
            "--plan-id",
            "D538-cli-contract",
            "--task-id",
            "APF3b-cli-contract",
            "--target-root",
            str(target_root),
            "--scope",
            "apf_backend",
            "--changed-file",
            "src/services/anonymous_preview_admission.py",
            "--dry-run",
            "--json",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["status"] == "policy_denied"
    assert any("config_contract_invalid" in reason for reason in payload["denied_reasons"])
    assert any("policy.allow_push" in reason for reason in payload["denied_reasons"])
    assert payload["target_writes_performed"] is False
    assert payload["git_push_performed"] is False
    assert payload["git_merge_performed"] is False
    assert payload["mcp_mutation_tools_exposed"] is False
    assert_no_target_git_side_effects(target_root)
```

### Task 6: Implement the minimal schema guard in `aiwg/git_steward.py`

**Objective:** Replace direct truthiness with literal-bool schema checks while preserving existing literal-true denial messages.

**Files:**

- Modify: `aiwg/git_steward.py`

**Expected import:**

```python
from aiwg.config import validate_policy_bool_schema
```

**Expected helper shape:**

```python
def _config_contract_denials(config: dict[str, Any]) -> list[str]:
    policy_schema = validate_policy_bool_schema(config, required_keys=_POLICY_MUTATION_FLAGS)
    denials = [f"config_contract_invalid: {error}" for error in policy_schema.errors]
    denials.extend(_git_bool_schema_denials(config))
    return denials


def _git_bool_schema_denials(config: dict[str, Any]) -> list[str]:
    git = config.get("git")
    if not isinstance(git, dict):
        return ["config_contract_invalid: git schema invalid: git must be a mapping"]

    errors: list[str] = []
    for key in _GIT_MUTATION_FLAGS:
        path = f"git.{key}"
        if key not in git:
            errors.append(f"{path} is required and must be literal bool")
            continue
        value = git[key]
        if type(value) is not bool:
            errors.append(f"{path} must be literal bool; got {type(value).__name__}")
    return [f"config_contract_invalid: {error}" for error in errors]
```

**Expected `_mutation_policy_denials()` shape:**

```python
def _mutation_policy_denials(config: dict[str, Any]) -> list[str]:
    contract_denials = _config_contract_denials(config)
    if contract_denials:
        return contract_denials

    policy = config["policy"]
    git = config["git"]
    denials: list[str] = []
    for key in _POLICY_MUTATION_FLAGS:
        if policy[key] is True:
            denials.append(f"{key}=true")
    for key in _GIT_MUTATION_FLAGS:
        if git[key] is True:
            denials.append(f"git.{key}=true")
    return denials
```

Notes:

- Use `is True` after schema validation for clarity.
- Do not use `bool(...)` for config mutation flags.
- Do not mutate config while validating.
- Do not broaden `validate_policy_bool_schema()` behavior unless CodeX asks for a shared helper.

### Task 7: Run targeted regression

**Objective:** Verify new D5.3.8 behavior and ensure existing Git Steward / D5.3.7 contracts still hold.

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/aiwg/git/test_d538_git_steward_policy_consumer_contract_guard.py \
  tests/aiwg/git/test_d4_git_steward_dry_run.py \
  tests/aiwg/test_d537_dispatch_policy_consumer_contract_guard.py \
  tests/aiwg/test_d536_runtime_policy_contract_guard.py \
  tests/aiwg/test_d535_policy_safety_config_validator.py \
  -p no:cacheprovider
```

Expected GREEN after implementation:

```text
all selected tests passed
```

### Task 8: Run full regression and safety probes

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli doctor --config aiwg.yaml
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.mcp.server --config aiwg.yaml --list-tools
```

Expected:

```text
full AIWG suite passes
AIWG doctor: OK
MCP tools exactly: status, list_tasks, get_task, recent_events
```

Business-repo boundary scan:

```bash
python - <<'PY'
from pathlib import Path
root = Path('D:/example/protected-business-repo')
patterns = ['D5.3.8', 'phase-d5-3-8', 'git steward policy consumer strict config alignment', 'D538']
hits = []
if root.exists():
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        for pattern in patterns:
            if pattern in text:
                hits.append((str(path), pattern))
print('hits=', len(hits))
PY
```

Expected:

```text
hits=0
```

### Task 9: Write D5.3.8 implementation acceptance artifact after implementation only

**Objective:** Capture implementation evidence without claiming CodeX review.

Future path:

```text
docs/ai-workgroup/state/artifacts/phase-d5-3-8-git-steward-policy-consumer-strict-config-alignment/acceptance.json
```

Expected initial implementation status:

```json
{
  "status": "completed_ready_for_codex_review",
  "codex_review": {
    "status": "pending",
    "passed": null
  }
}
```

Do not write this implementation artifact during planning-only unless implementation is actually performed.

## Acceptance criteria for D5.3.8 implementation

Implementation is acceptable only if all are true:

- New D5.3.8 tests first fail for expected reasons before production changes.
- `policy.*` mutation flags require literal bool values.
- `git.*` mutation flags require literal bool values.
- Missing required policy/git mutation flags fail closed as `config_contract_invalid`.
- Non-mapping policy/git sections fail closed as `config_contract_invalid`.
- Literal `False` defaults still allow the existing dry-run planned path.
- Literal `True` mutation switches keep existing denial messages such as `allow_push=true` and `git.allow_auto_commit=true`.
- `git-plan` remains `--dry-run` only.
- No real Git worktree, commit, push, merge, PR creation, PR comment, or GitHub mutation occurs.
- MCP tool surface remains read-only.
- Protected business repository remains untouched.
- CodeX Automations remain untouched.

## CodeX review focus for this planning artifact

Ask CodeX to verify:

1. The planned scope is narrow enough: `aiwg/git_steward.py` plus new D5.3.8 tests only by default.
2. The plan correctly targets the direct truthiness risk in `_mutation_policy_denials()`.
3. Treating missing `policy.*` / `git.*` mutation flags as `config_contract_invalid` is acceptable for D5.3.x strict alignment.
4. The plan preserves literal-true denial semantics and literal-false dry-run behavior.
5. The plan does not authorize implementation, real agents, MCP mutation tools, protected business-repo writes, GitHub writes, push, merge, deploy, or CodeX Automation modification.

## Planning-only verification checklist

For this planning-only slice, verify only:

```text
planning_doc_written=true
planning_acceptance_artifact_written=true
runtime_files_modified=false
test_files_modified=false
config_files_modified=false
business_repository_files_modified=false
implementation_started=false
```

Suggested planning verification commands:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli doctor --config aiwg.yaml
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.mcp.server --config aiwg.yaml --list-tools
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/aiwg/git/test_d4_git_steward_dry_run.py -p no:cacheprovider
```

## Recommended next after planning review

If CodeX approves this planning artifact, write back only this planning acceptance artifact's `status` and `codex_review` fields to `codex_review_passed`, then wait for explicit authorization before D5.3.8 implementation. Continue to keep real agents, MCP mutation tools, protected business-repository writes, GitHub writes, push, merge, deploy, and CodeX Automation modification disabled.
