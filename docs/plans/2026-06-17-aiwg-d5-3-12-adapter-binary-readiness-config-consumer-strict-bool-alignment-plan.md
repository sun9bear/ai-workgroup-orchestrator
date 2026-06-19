# AIWG D5.3.12 Adapter Binary Readiness Config Consumer Strict Bool Alignment Plan

> Status: `planning_only`
> Generated at: `2026-06-17T00:20:14Z`
> Repaired at: `2026-06-17T00:48:00Z`
> Upstream gate: D5.3.11 implementation is `codex_review_passed`.
> Boundary: this phase writes only planning artifacts. It does not start implementation.

## Encoding repair note

CodeX reviewed the first D5.3.12 planning draft and agreed with the selected direction, but reported Chinese mojibake in the Markdown body. To make the execution plan safe for later Hermes/CodeX/agent readers that may traverse a non-UTF-8 path, this repaired draft intentionally uses ASCII-only prose while preserving the same planning contract.

This repair does not mark CodeX review as passed. The D5.3.12 planning acceptance remains `completed_ready_for_codex_review` with `codex_review.status = pending` until CodeX performs the quick recheck.

## 0. Current gate

D5.3.11 current gate:

```text
docs/ai-workgroup/state/artifacts/phase-d5-3-11-orchestrator-runner-policy-consumer-strict-config-alignment/acceptance.json
status = codex_review_passed
codex_review.status = passed
codex_review.passed = true
```

D5.3.11 already removed the targeted runner policy truthiness risk in `aiwg/runners/orchestrator.py`:

- `_policy_bool()` / target `bool(policy.get(...))` usage was removed from the runner policy path.
- `stale_claim_requires_human`, `auto_retry_needs_revision`, and `auto_retry_write_tasks` are now literal-bool contract consumers.
- malformed runner policy fails closed before database/inbox/stale-claim/retry/claim/adapter side effects.
- real agents, MCP mutation tools, and protected business repository writes remain disabled.

## 1. Safety boundary for D5.3.12 planning-only

Allowed files for this planning-only step:

```text
docs/plans/2026-06-17-aiwg-d5-3-12-adapter-binary-readiness-config-consumer-strict-bool-alignment-plan.md
docs/ai-workgroup/state/artifacts/phase-d5-3-12-adapter-binary-readiness-config-consumer-strict-bool-alignment-planning/acceptance.json
```

Explicitly forbidden during planning-only:

- D5.3.12 implementation;
- runtime code changes;
- test code changes;
- `aiwg.yaml` changes;
- real agents or external agents;
- real adapter task process execution;
- unreviewed version-probe enablement;
- MCP mutation tools;
- protected AIVideoTrans business repository writes;
- GitHub write, PR mutation, or PR comments;
- `git commit`, `git push`, or `git merge`;
- deployment;
- CodeX Automation modification.

## 2. Why D5.3.12 selects adapter binary readiness

D5.3.5 deferred `adapter_binary_readiness` as a small later schema surface. D5.3.6 through D5.3.11 then covered higher-risk runtime policy, dispatch, Git Steward, D5 preflight, real-adapter process/executor/sandbox, and orchestrator runner policy consumers.

The next minimal high-value slice is B12 adapter binary readiness because it still directly consumes config booleans with `bool(...)`, and one of those values can trigger a bounded subprocess version probe.

Current static reconnaissance:

```text
aiwg/adapter_binary_readiness.py:35: global_probe_enabled = bool(readiness.get("version_probe_enabled", False))
aiwg/adapter_binary_readiness.py:48: per_adapter_probe_enabled = bool(override.get("version_probe_enabled", global_probe_enabled))
aiwg/adapter_binary_readiness.py:122: "configured_auto_install": bool(readiness.get("auto_install", False)),
aiwg/adapter_binary_readiness.py:123: "configured_auto_login": bool(readiness.get("auto_login", False)),
aiwg/adapter_binary_readiness.py:124: "configured_read_tokens": bool(readiness.get("read_tokens", False)),
```

Risk summary:

1. `adapter_binary_readiness.version_probe_enabled = "false"` is truthy in Python. If a binary is available and `run_version_probes=True`, current code may start a bounded `--version` subprocess.
2. `adapter_binary_readiness.adapters.<adapter>.version_probe_enabled = "false"` is also truthy and can override a safe global default.
3. `auto_install`, `auto_login`, and `read_tokens` currently do not trigger install/login/token-read behavior, but `bool("false") == True` pollutes readiness metadata and makes doctor/CLI semantics inconsistent.
4. checked-in `aiwg.yaml` omits `adapter_binary_readiness`, so this slice must keep absent-compatible safe defaults instead of requiring a config-file rewrite.

## 3. Selected D5.3.12 scope

Primary target:

```text
aiwg/adapter_binary_readiness.py::resolve_adapter_binary_readiness
aiwg/adapter_binary_readiness.py::write_adapter_binary_readiness_report
```

Related entrypoints that future implementation should consider after CodeX approval:

```text
aiwg/config.py::validate_config_contract
aiwg/doctor.py::run_doctor
aiwg/cli.py::_adapter_readiness_command
```

Reason to include the related entrypoints in review scope:

- `doctor` calls `resolve_adapter_binary_readiness(..., run_version_probes=False)` and should report malformed readiness config as `config_contract_invalid` instead of silently relying on truthiness.
- `adapter-readiness` CLI currently calls `init_database(...)` before `write_adapter_binary_readiness_report(...)`. Future implementation must define exactly where the fail-closed boundary sits.

Expected implementation files after explicit Human/CodeX authorization:

```text
aiwg/config.py
aiwg/adapter_binary_readiness.py
aiwg/doctor.py
aiwg/cli.py
tests/aiwg/runners/test_d5312_adapter_binary_readiness_config_consumer_contract_guard.py
```

Optional only if CodeX widens scope:

```text
aiwg.yaml
aiwg/adapter_readiness_gate.py
tests/aiwg/runners/test_b12_adapter_binary_readiness.py
```

`aiwg.yaml` should not be changed in planning and should not be required for implementation unless CodeX explicitly requests safe-default config alignment.

## 4. Planned behavior contract

### 4.1 Absent-compatible defaults

Because checked-in `aiwg.yaml` omits `adapter_binary_readiness`, future implementation should preserve current safe absent behavior:

```python
ADAPTER_BINARY_READINESS_BOOL_DEFAULTS = {
    "auto_install": False,
    "auto_login": False,
    "read_tokens": False,
    "version_probe_enabled": False,
}
```

Rules:

- missing top-level `adapter_binary_readiness` means the safe defaults above;
- missing individual keys also use safe defaults;
- if `adapter_binary_readiness` is present, it must be a mapping;
- if any selected bool key is present, its value must be literal `bool` (`type(value) is bool`);
- string values such as `"false"`, `"true"`, and `"0"`, integers such as `0` and `1`, `None`, lists, and objects fail closed as malformed config.

### 4.2 Per-adapter overrides

For:

```text
adapter_binary_readiness.adapters.<adapter>.version_probe_enabled
```

Rules:

- missing `adapters` means `{}`;
- if `adapters` is present, it must be a mapping;
- each adapter override, if present, must be a mapping;
- missing per-adapter `version_probe_enabled` inherits global `version_probe_enabled`;
- present per-adapter `version_probe_enabled` must be literal `bool`;
- non-literal values fail closed before any version-probe subprocess can start.

### 4.3 Safety-value enforcement remains separate from literal-bool schema

Literal schema validation should not itself forbid `version_probe_enabled=True`; B12 already allows bounded version probes when explicitly configured.

Safety values for dangerous side effects remain false:

```text
auto_install = false
auto_login = false
read_tokens = false
```

Future implementation should keep `doctor` enforcing these values as false, without using permissive `bool(...)` coercion. A literal `True` remains a safety error. A string `"false"` is a schema error, not a safe false value and not an unsafe true value.

### 4.4 Malformed-config outcome

Future implementation should produce a machine-readable failure with:

```text
error = config_contract_invalid
```

Expected error examples:

```text
config_contract_invalid: adapter_binary_readiness must be a mapping
config_contract_invalid: adapter_binary_readiness.version_probe_enabled must be literal bool; got str
config_contract_invalid: adapter_binary_readiness.auto_install must be literal bool; got int
config_contract_invalid: adapter_binary_readiness.adapters.opencode.version_probe_enabled must be literal bool; got str
```

Minimum fail-closed ordering requirement:

- malformed readiness config must be detected before any version-probe subprocess starts;
- malformed readiness config must not allow install/login/token-read side effects;
- malformed readiness config must not expose MCP mutation tools or enable real adapter dispatch.

Implementation-boundary question for CodeX/Human before RED tests are finalized:

- Minimum contract: malformed `adapter_binary_readiness` fails closed before any version-probe subprocess.
- Stricter contract: malformed `adapter_binary_readiness` fails closed before `adapter-readiness` CLI performs `init_database(...)`, event insertion, or readiness report writes.
- If the stricter no-DB/no-event/no-report behavior is required, it must be written into the D5.3.12 RED tests explicitly; otherwise implementation may only enforce the subprocess-before-fail boundary and report a blocked readiness result.

Recommendation: require at least the minimum subprocess-before-fail boundary. Prefer the stricter no-DB/no-event/no-report boundary only if CodeX confirms it is part of D5.3.12 implementation scope.

## 5. Planned RED tests for future implementation

These tests must not be added until CodeX passes this planning artifact and Human/CodeX explicitly authorizes implementation.

### Test 1 - absent-compatible safe default

File:

```text
tests/aiwg/runners/test_d5312_adapter_binary_readiness_config_consumer_contract_guard.py
```

Scenario:

- Build a checked-in-like minimal config with no `adapter_binary_readiness` section.
- Call `resolve_adapter_binary_readiness(..., run_version_probes=True)`.
- Assert no config error.
- Assert global `version_probe_enabled is False`.
- Assert no version-probe subprocess starts unless a literal true value is explicitly configured.
- Assert no install/login/token-read side effects occur.

### Test 2 - top-level readiness must be a mapping

Scenario:

```python
config["adapter_binary_readiness"] = ["not", "mapping"]
```

Expected:

- `resolve_adapter_binary_readiness(...)` or CLI fails closed with `config_contract_invalid`.
- No version-probe subprocess starts.
- No install/login/token-read side effects occur.

### Test 3 - global version_probe_enabled string false must not start subprocess

Scenario:

```python
config["adapter_binary_readiness"] = {
    "version_probe_enabled": "false",
    "adapters": {
        "opencode": {
            "path": sys.executable,
            "version_args": ["-c", "print('SHOULD_NOT_RUN')"],
        }
    },
}
```

Expected before GREEN: current code treats `"false"` as true and may run the version probe.

Expected after GREEN:

- `config_contract_invalid`;
- no version-probe subprocess;
- error path names `adapter_binary_readiness.version_probe_enabled`.

### Test 4 - per-adapter version_probe_enabled string false must not start subprocess

Scenario:

```python
config["adapter_binary_readiness"] = {
    "version_probe_enabled": False,
    "adapters": {
        "opencode": {
            "path": sys.executable,
            "version_probe_enabled": "false",
            "version_args": ["-c", "print('SHOULD_NOT_RUN')"],
        }
    },
}
```

Expected:

- `config_contract_invalid`;
- no version-probe subprocess;
- error path names `adapter_binary_readiness.adapters.opencode.version_probe_enabled`.

### Test 5 - auto_install, auto_login, and read_tokens non-literal values are schema errors

Parametrize:

```python
("auto_install", "false")
("auto_login", 0)
("read_tokens", None)
```

Expected:

- schema error uses the exact key path;
- `doctor` returns failed result, not OK;
- no install/login/token-read side effects occur.

### Test 6 - adapter-readiness CLI malformed config behavior

Scenario:

- Write temporary config with `adapter_binary_readiness.version_probe_enabled: "false"`.
- Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m aiwg.cli adapter-readiness --config <tmp>/aiwg.yaml --json
```

Expected after CodeX confirms exact CLI behavior:

- either nonzero exit with `config_contract_invalid`; or
- JSON blocked report with `status=blocked` / `error=config_contract_invalid` and no version-probe subprocess/event.

If CodeX chooses the stricter contract, this test must additionally assert no database initialization, no event insertion, and no readiness report write for malformed config.

## 6. GREEN implementation guidance after explicit authorization

Do not implement during planning. If CodeX passes this planning artifact and Human/CodeX authorizes implementation, use strict TDD:

1. Add D5.3.12 RED tests first and confirm the intended failures.
2. Before writing RED tests for CLI behavior, record CodeX/Human decision on the minimum vs stricter malformed-config boundary.
3. Add a small schema helper, likely in `aiwg/config.py` or local to `aiwg/adapter_binary_readiness.py`, for absent-compatible literal-bool validation.
4. Replace the five target `bool(readiness.get(...))` and `bool(override.get(...))` sites with already-validated values.
5. Make `doctor` surface schema errors without permissive `_as_bool(...)` for adapter readiness keys.
6. Make `adapter-readiness` CLI fail closed according to the CodeX-reviewed behavior.
7. Run targeted D5.3.12 tests, B12 regression, doctor, MCP list-tools, and full `tests/aiwg`.
8. Write implementation acceptance as `completed_ready_for_codex_review`; do not mark CodeX passed until review results are provided.

## 7. Non-goals

Do not include in D5.3.12 implementation unless CodeX explicitly widens scope:

- broad full-config schema migration;
- `aiwg.yaml` safe-default alignment;
- real adapter dispatch enablement;
- real adapter task process execution;
- adapter readiness gate (`aiwg/adapter_readiness_gate.py`) config hardening;
- dashboard/status payload bool normalization;
- task/frontmatter bool normalization (`can_write`, `requires_human`);
- artifact-root/path-boundary refactors;
- MCP mutation tools;
- protected business repository writes.

## 8. Planning acceptance criteria

This planning-only phase is ready for CodeX quick recheck when:

1. This repaired plan exists under `docs/plans/` and has no mojibake patterns.
2. Planning acceptance exists under `docs/ai-workgroup/state/artifacts/phase-d5-3-12-adapter-binary-readiness-config-consumer-strict-bool-alignment-planning/acceptance.json`.
3. D5.3.11 implementation acceptance is `codex_review_passed`.
4. Static reconnaissance records the five targeted adapter-readiness truthiness consumers.
5. `doctor` remains OK for checked-in `aiwg.yaml`.
6. MCP `--list-tools` remains read-only: `status`, `list_tasks`, `get_task`, `recent_events`.
7. Protected AIVideoTrans marker scan for D5.3.12 terms has `0 hits`.
8. No runtime/test/config/business-repo files are changed by this planning-only step.
9. `codex_review` remains pending until CodeX performs the quick recheck.

## 9. CodeX quick recheck focus

Ask CodeX to confirm:

1. The repaired Markdown body is readable and no longer contains mojibake.
2. D5.3.12 should target `adapter_binary_readiness` bool consumers next.
3. Absent-compatible defaults are correct because checked-in `aiwg.yaml` lacks the section.
4. `version_probe_enabled="false"` is the highest-risk RED case because it can start a bounded subprocess through Python truthiness.
5. `auto_install`, `auto_login`, and `read_tokens` should be literal-bool schema-checked while still requiring literal false for safety.
6. Future implementation should include `doctor` and `adapter-readiness` CLI behavior, or explicitly narrow to resolver/report functions only.
7. Whether malformed CLI config must fail before `init_database(...)`, event insertion, and readiness report write, or whether the minimum subprocess-before-fail boundary is sufficient.
8. Future implementation must wait for explicit Human/CodeX authorization after planning review passes.

## 10. Recommended next

Submit this repaired D5.3.12 planning-only artifact to CodeX quick recheck. If CodeX passes, still wait for explicit Human/CodeX implementation authorization before adding RED tests or changing runtime behavior.
