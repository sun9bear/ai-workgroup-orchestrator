# AIWG D5.3 Planning: Unified Evidence Boundary Audit

> **状态：planning_only**
> **Generated at:** `2026-06-07T12:41:05Z`
> **上游门禁：** D5.2 已由 independent review + CodeX quick review 双通过并写回 `codex_review_passed`。

## 0. Safety boundary

D5.3 当前只做“画地图”，不做大规模路径重构，不启用真实执行。

严格禁止：

- real agents / external agents
- MCP mutation tools
- GitHub write API
- PR mutation / PR comment
- `D:/example/protected-business-repo` 业务仓写入
- deployment
- CodeX Automation 修改

允许的本次 planning-only 产物只在 Orchestrator 内：

- `docs/plans/2026-06-07-aiwg-d5-3-evidence-boundary-audit-plan.md`
- `docs/ai-workgroup/state/artifacts/phase-d5-3-evidence-boundary-audit/audit-matrix.json`

## 1. Goal

统一梳理 Orchestrator 中会写 artifact / evidence / audit / acceptance / ledger 的路径，判断：

1. 当前写入点清单；
2. 每个写入点的 root 解析方式；
3. 是否已经保证 under Orchestrator `docs/ai-workgroup/state/artifacts`；
4. 是否已经保证 not under / not overlap AIVideoTrans target root；
5. 风险等级 `P0/P1/P2/P3`；
6. 下一步最小 hardening slice。

D5.3 的核心策略：**先审计矩阵，后硬化。** 不在 planning 阶段直接改全项目路径守门。

## 2. Boundary standard to converge on

D5.2 新增的 `aiwg.evidence_paths` 是后续统一守门标准：

- `assert_orchestrator_artifact_root(artifact_root, project_root, target_roots=[...])`
- `assert_orchestrator_evidence_path(path, project_root, evidence_base=artifact_root, target_roots=[...])`

期望语义：

- artifact/evidence/audit/ledger 写入根必须落在 Orchestrator 的 `docs/ai-workgroup/state/artifacts` 下，除非该类别明确设计为 `state_db` 或 workgroup message，并有单独守门；
- 所有 protected target roots（本轮参考：`D:/example/protected-business-repo`）不得与写入根或最终写入路径发生任一方向 overlap；
- 失败应 fail-closed，错误码清楚，例如 `artifact_root_outside_orchestrator_artifacts`、`artifact_root_overlaps_target_root`、`evidence_path_outside_orchestrator_evidence`。

## 3. Scan scope

本轮纳入：

- `aiwg/**/*.py` runtime package；
- `scripts/ai-workgroup/human-dashboard-server.py` 仅作为 operational side-effect reference；
- D5.2 acceptance 状态和既有 plans/guides 作为上下文。

本轮不作为 runtime hardening 对象：

- `tests/**/*.py`；
- `.hermes_tmp_run_pilot*.py` 一次性历史脚本；
- `docs/ai-workgroup/quarantine/**` 里隔离保存的历史目标代码片段。

## 4. Risk rubric

| Level | Meaning |
| --- | --- |
| `P0` | 当前路径可现实地写入 protected target 或绕过真实执行门禁；下一步执行前必须先挡住。 |
| `P1` | runtime artifact/evidence/audit/ledger writer 未使用统一 guard 或未直接检查 target overlap；当前 config 安全但误配置可外溢。 |
| `P2` | 有本地/部分 guard，或是 manual/document-only 产物，但未统一到 `aiwg.evidence_paths`；存在漂移风险。 |
| `P3` | 已使用 `aiwg.evidence_paths`，或为只读 / 非 runtime 项；当前不优先处理。 |

## 5. Audit matrix summary

完整机器可读矩阵见：

```text
docs/ai-workgroup/state/artifacts/phase-d5-3-evidence-boundary-audit/audit-matrix.json
```

| ID | Write point | Current root resolution | Unified guard? | Under Orchestrator artifacts? | Not overlap target? | Risk | Recommendation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D53-M01 | `aiwg/d5_preflight.py` D5 preflight artifact + provenance | `resolve_config_path` → `assert_orchestrator_artifact_root` → `assert_orchestrator_evidence_path` | yes | yes | yes | P3 | Keep as reference implementation. |
| D53-M02 | `aiwg/git_steward.py` Git dry-run plan artifacts | `resolve_config_path('artifact_root')` + local `_path_is_relative_to` / `_paths_overlap` | no | yes, local | yes, local | P2 | Later replace local helpers with `aiwg.evidence_paths`. |
| D53-M03 | `aiwg/workflow_preflight.py` workflow fake output artifacts | `resolve_config_path('artifact_root')` + local workflow/step/output checks | no | yes, local | yes, local | P2 | Later converge local guard to shared helper. |
| D53-M04 | `aiwg/write_gate.py` D1 audit JSON + write-gate ledger | `config['artifact_root']` or default under `orchestrator_root`; outside root falls back to safe base | no | yes after fallback | partial | P2 | Decide fail-closed vs safe-fallback semantics, then unify. |
| D53-M05 | `aiwg/adapter_registry.py` adapter preflight manifest/prompt | `resolve_config_path('artifact_root')/<agent>/<task>` | no | no direct | no direct | P1 | First hardening candidate. |
| D53-M06 | `aiwg/runners/fake.py` fake adapter report/stdout/stderr | `resolve_config_path('artifact_root')/Fake/<task>` | no | no direct | no direct | P1 | Harden with adapter_registry. |
| D53-M07 | `aiwg/verification.py` verification stdout/stderr | `resolve_config_path('artifact_root')/<agent>/<task>/verification` | no | no direct | no direct | P1 | Add fail-closed tests before changing because it captures shell output. |
| D53-M08 | `aiwg/adapter_binary_readiness.py` readiness report | `resolve_config_path('artifact_root')/_adapter-readiness` | no | no direct | no direct | P1 | Harden with shared artifact-root helper. |
| D53-M09 | `aiwg/real_adapter_executor.py` dry-run stdout/stderr/report | manifest `artifacts.prompt_path.parent` or `manifest_path.parent` | no | transitive only | transitive only | P1 | Add local manifest-chain assertions after upstream manifest writer is guarded. |
| D53-M10 | `aiwg/real_adapter_sandbox.py` invocation plan | manifest `artifacts.prompt_path.parent` or `manifest_path.parent` | no | transitive only | transitive only | P1 | Guard manifest-derived `artifact_dir` before writing. |
| D53-M11 | `aiwg/real_adapter_process.py` sandbox probe stdout/stderr/report | manifest `artifacts.prompt_path.parent` or `manifest_path.parent` | no | transitive only | transitive only | P1 | Must be guarded before any future process-enabled slice. |
| D53-M12 | `aiwg/operator_approval.py` real-start authorization/revocation JSON | probe report parent or payload `real_start_authorization_path` | no | transitive only | transitive only | P1 now / P0 before real mode | Explicit guard required before any real-mode milestone. |
| D53-M13 | `aiwg/state/importer.py::legacy_audit` legacy markdown report | absolute `legacy_migration.report_path` or relative to project root | no | default under state, not artifacts; configurable escape | no direct | P1 | Decide guarded state/evidence base or move under artifacts. |
| D53-M14 | `aiwg/state/database.py` state DB + WAL/SHM ledger | `resolve_config_path('state_db')`; `connect_database` creates parent | no | not global | not global | P1 | Plan separate state DB guard; many callers rely on this. |
| D53-M15 | manual `phase-*/acceptance.json` artifacts | explicit Hermes/tool paths under Orchestrator | no | process convention | process convention | P2 | Keep manual explicit for now; guard if generation becomes CLI. |
| D53-M16 | `scripts/ai-workgroup/human-dashboard-server.py` workgroup messages | configured workgroup root / inbox / status updates | no | outside artifact scope | not evaluated | P2 outside D5.3 | Track separately as workgroup-message boundary audit. |
| D53-M17 | `.hermes_tmp_run_pilot*.py` / quarantine snippets | hard-coded historical paths | no | mostly historical | not active runtime | P3 | Do not refactor in D5.3. |

## 6. Findings

### 6.1 Already good reference

- `aiwg/d5_preflight.py` is the only audited runtime writer already on the new `aiwg.evidence_paths` guard.
- It checks both the configured `artifact_root` and final D5 artifact path against Orchestrator artifact base and target-root overlap.

### 6.2 Duplicated local guards exist

These are mostly safe today but should converge to one implementation to avoid semantic drift:

- `aiwg/git_steward.py`
- `aiwg/workflow_preflight.py`
- `aiwg/write_gate.py`

### 6.3 Unguarded P1 cluster

These runtime writers still rely on current config correctness or transitive manifest paths:

- adapter preflight manifest/prompt;
- fake adapter reports;
- verification stdout/stderr;
- adapter binary readiness report;
- real-adapter dry-run / sandbox / process chain outputs;
- real-start authorization artifacts;
- legacy migration report path;
- central `state_db` initialization.

No evidence was found that current D5.2/D5.3 state wrote any of these into AIVideoTrans. The finding is about **future misconfiguration/feature enablement risk**, not a current observed target write.

## 7. Recommended minimal hardening slice

### D5.3.1 — RED fail-closed tests first

Write focused tests only; no behavior changes yet.

Representative tests:

1. `adapter_registry` rejects `artifact_root` under target root.
2. `FakeAdapter` rejects `artifact_root` under target root.
3. `verification` rejects `artifact_root` under target root without running commands.
4. `adapter_binary_readiness` rejects `artifact_root` under target root with version probes disabled.
5. `legacy_audit` rejects absolute `legacy_migration.report_path` under target root.
6. `state_db` guard design test rejects `state_db` under target root for guarded callers.

### D5.3.2 — Small shared helper adoption for P1 artifact-root writers

Add a small wrapper around `aiwg.evidence_paths.assert_orchestrator_artifact_root` and use it in:

- `adapter_registry.py`
- `runners/fake.py`
- `verification.py`
- `adapter_binary_readiness.py`

Keep all existing safety flags false. Do not enable real execution.

### D5.3.3 — Manifest-chain assertions

Once upstream manifest/prompt paths are guarded, add local assertions before downstream writes in:

- `real_adapter_executor.py`
- `real_adapter_sandbox.py`
- `real_adapter_process.py`
- `operator_approval.py`

This protects against manually supplied or stale manifest artifacts.

### D5.3.4 — Converge local guards

After P1 writers are covered, replace duplicated local helpers in:

- `git_steward.py`
- `workflow_preflight.py`
- `write_gate.py`

Preserve behavior unless a test explicitly approves safer fail-closed behavior.

## 8. Non-goals for next slice

Do **not** include in the immediate hardening slice:

- real adapter execution;
- GitHub write or PR mutation;
- CodeX Automation changes;
- AIVideoTrans business repo writes;
- broad refactor of every path helper;
- cleanup of historical `.hermes_tmp_run_pilot*.py` scripts;
- human-dashboard workgroup message boundary changes.

## 9. Planning acceptance criteria

- This plan exists under `docs/plans/`.
- Audit matrix JSON exists under Orchestrator `docs/ai-workgroup/state/artifacts/phase-d5-3-evidence-boundary-audit/`.
- No runtime code changes are required for D5.3 planning-only.
- JSON validates with `python -m json.tool`.
- Doctor remains OK and MCP surface remains read-only if checked.
- Target repo contains no `D5.3` / `phase-d5-3` / `evidence-boundary-audit` marker writes.
