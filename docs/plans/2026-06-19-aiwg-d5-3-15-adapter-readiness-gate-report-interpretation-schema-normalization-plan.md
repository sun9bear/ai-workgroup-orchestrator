# AIWG D5.3.15 Adapter Readiness Gate Report Interpretation / Schema-Normalization Plan

> Status: `planning_only`
> Generated at: `2026-06-19T10:06:49Z`
> Upstream gate: D5.3.14 implementation and post-merge reconciliation are CodeX-reviewed and passed.
> Boundary: this phase writes only this planning document plus a local planning acceptance artifact. It does **not** start implementation and does **not** add RED tests.

## 0. Current gate

D5.3.14 implementation is closed and merged to `main`:

```text
main HEAD = 9f99f9331edff383b2409445b56950ea982185e2
origin/main = 9f99f9331edff383b2409445b56950ea982185e2
PR #1 = merged
D5.3.14 acceptance status = codex_review_passed
D5.3.14 codex_review.passed = true
```

Post-merge reconciliation was also reviewed by CodeX and passed. The current phase is therefore allowed to move only into the next planning gate:

```text
D5.3.15 planning-only
No implementation
No RED tests
No runtime/test/config/business-repo edits
```

## 1. Why D5.3.15 is the next planning slice

D5.3.14 fixed the gate-level `adapter_readiness_gate.enabled` truthiness bypass. During review, CodeX also found a related report-semantics edge: an old blocked readiness report could be misinterpreted later as an adapter lookup failure if report-level blocked/error semantics were not preserved before adapter lookup.

The D5.3.14 P2 fix addressed that concrete case. D5.3.15 should now systematize the contract so future implementation is not a one-off patch.

Selected current code in `aiwg/adapter_readiness_gate.py`:

```text
line 26   _readiness_report_block(report)
line 27   status = str(report.get("status") or "")
line 28   error = str(report.get("error") or "")
line 195  report_block = _readiness_report_block(report)
line 206  adapters = report.get("adapters") if isinstance(report.get("adapters"), dict) else {}
line 207  adapter_doc = adapters.get(manifest_adapter_type)
line 208  if not isinstance(adapter_doc, dict): adapter_readiness_adapter_missing
line 218  if not bool(adapter_doc.get("available", False)): adapter_binary_missing
line 394  _validate_codex_lock(...)
line 406  report_desktop_allowed = bool(report_codex.get("desktop_automation_allowed", False))
line 407  manifest_desktop_allowed = bool(manifest_codex.get("desktop_automation_allowed", False))
```

D5.3.15 should not jump directly to code. It should define the interpretation order and payload contract first, then ask CodeX to review the contract before any tests are added.

## 2. Planning-only safety boundary

Allowed files for this planning-only step:

```text
docs/plans/2026-06-19-aiwg-d5-3-15-adapter-readiness-gate-report-interpretation-schema-normalization-plan.md
docs/ai-workgroup/state/artifacts/phase-d5-3-15-adapter-readiness-gate-report-interpretation-schema-normalization-planning/acceptance.json
```

Explicitly forbidden in this planning-only step:

- D5.3.15 implementation;
- RED tests or any test-file changes;
- runtime code changes under `aiwg/`;
- `aiwg.yaml` or config-file changes;
- `adapter_binary_readiness.py` report-writer changes;
- real agents or external agents;
- real adapter task process execution;
- MCP mutation tools;
- GitHub write APIs, PR comments, PR mutation, `git push`, or `git merge`;
- protected AIVideoTrans business repository writes;
- deployment;
- CodeX Automation modification.

## 3. Current report interpretation order

Current `evaluate_adapter_readiness_gate(...)` order, simplified:

```text
1. Validate adapter_readiness_gate.enabled schema.
2. Allow explicit literal enabled=false skip.
3. Skip if execution_mode is not required.
4. Build base payload with started_real_process=false and started_adapter_process=false.
5. Block manifest adapter-type mismatch.
6. Locate latest adapter_binary_readiness_checked event.
7. Block missing/stale/unreadable report.
8. Block schema_version mismatch.
9. Resolve current adapter binary readiness with run_version_probes=false.
10. Interpret report-level blocked/error via _readiness_report_block(report).
11. Read report.adapters as mapping, else silently use {}.
12. Look up adapters[manifest_adapter_type].
13. Treat non-mapping/missing adapter_doc as adapter_readiness_adapter_missing.
14. Evaluate adapter_doc.available with bool(...).
15. Compare reported/current resolved paths.
16. Validate Codex automation lock with bool(...) on desktop_automation_allowed fields.
17. Allow gate pass.
```

D5.3.14 already made step 1 strict. D5.3.15 should plan the report-content half: steps 10-14, with a narrow note on step 16 as an explicit non-goal unless CodeX widens the slice.

## 4. Risks being planned for

### 4.1 Report-level status/error can be overwritten by later interpretation

A readiness report may intentionally be a blocked report:

```json
{
  "schema_version": "aiwg.adapter_binary_readiness.v1",
  "status": "blocked",
  "error": "config_contract_invalid",
  "errors": ["config_contract_invalid: adapter_binary_readiness.version_probe_enabled must be literal bool; got str"],
  "adapters": {}
}
```

This report is not saying that a specific adapter is missing. It is saying the whole readiness resolution was blocked. The gate must preserve that report-level semantic before any adapter lookup.

D5.3.14 fixed the known old-report case. D5.3.15 should make the ordered contract explicit and future-proof.

### 4.2 `adapters` shape is currently collapsed to `{}`

Current code:

```python
adapters = report.get("adapters") if isinstance(report.get("adapters"), dict) else {}
```

For a non-blocked report, this silently maps malformed `adapters` to an adapter-missing result. That makes a schema problem look like a missing adapter.

### 4.3 `adapter_doc.available` currently uses truthiness

Current code:

```python
if not bool(adapter_doc.get("available", False)):
    return _blocked("adapter_binary_missing", ...)
```

This can misinterpret malformed values:

```text
"false"  -> truthy, can incorrectly pass the availability branch
0        -> falsey, becomes adapter_binary_missing instead of report-schema invalid
None     -> falsey, becomes adapter_binary_missing instead of report-schema invalid
missing  -> falsey, becomes adapter_binary_missing instead of report-schema invalid
[] / {}  -> falsey, becomes adapter_binary_missing instead of report-schema invalid
```

The report writer currently emits literal booleans, but the gate is an execution boundary and should fail closed on malformed persisted reports.

## 5. Proposed D5.3.15 planning scope

D5.3.15 should plan one narrow future implementation slice:

```text
Normalize and fail-close adapter_readiness_gate interpretation of readiness report content before adapter lookup/availability decisions.
```

Included in scope for future implementation after CodeX/Human authorization:

1. Define a small helper in `aiwg/adapter_readiness_gate.py` for report-content normalization.
2. Preserve report-level blocked/error semantics before adapter lookup.
3. Validate non-blocked report `adapters` shape before using it.
4. Distinguish missing adapter entry from malformed adapter entry.
5. Require `adapter_doc.available` to be a literal `bool` before using it.
6. Preserve existing `adapter_binary_missing` behavior for literal `available=false`.
7. Preserve existing happy path for literal `available=true` with matching current binary path.
8. Preserve `started_real_process=false` and `started_adapter_process=false` in every blocked payload.
9. Cover direct gate, `resume_preflight(...)`, and `approve_real_start(...)` in future tests.

Not included unless CodeX explicitly widens scope:

- `_required_modes(...)` schema cleanup;
- `_max_age_minutes(...)` schema cleanup;
- Codex `desktop_automation_allowed` bool normalization in `_validate_codex_lock(...)`;
- report writer schema changes in `aiwg/adapter_binary_readiness.py`;
- dashboard/status API changes;
- MCP mutation tools;
- real execution enablement;
- protected business repository writes;
- CodeX Automation modification.

## 6. Planned interpretation order contract

Future implementation should make the order explicit. Proposed order after report JSON is loaded and `schema_version` is accepted:

```text
A. Interpret report-level blocked/error semantics.
B. Only if the report is not blocked/error, normalize non-blocked report shape.
C. Refresh current readiness with run_version_probes=false.
D. Validate adapter lookup and adapter_doc shape.
E. Validate adapter_doc.available as literal bool.
F. Continue existing current-path comparison and Codex lock checks.
```

### 6.1 Report-level blocked/error priority

A report is report-level blocked if either condition holds:

```text
report.status == "blocked"
report.error is a non-empty string
```

Expected blocked result:

```text
allowed = false
reason = report.error if non-empty else "adapter_readiness_report_blocked"
payload.reason = same as reason
payload.error = same as reason
payload.errors = report.errors if it is a list, otherwise [reason]
payload.report_status = report.status
payload.started_real_process = false
payload.started_adapter_process = false
```

This result must be returned before any `adapters` or `adapter_doc.available` interpretation.

Rationale: a report-level block is a whole-report semantic. It is not an adapter lookup failure.

### 6.2 Non-blocked report shape normalization

For a report that is not blocked/error, future implementation should fail closed on malformed report shape.

Recommended new reason name:

```text
adapter_readiness_report_schema_invalid
```

Recommended payload fields:

```text
reason = "adapter_readiness_report_schema_invalid"
error = "adapter_readiness_report_schema_invalid"
errors = [specific field error strings]
started_real_process = false
started_adapter_process = false
readiness_report_path = <path>
readiness_event_id = <event id>
readiness_created_at = <timestamp>
```

Specific errors should include field paths, for example:

```text
report.adapters must be a mapping
report.adapters.<adapter_type> must be a mapping
report.adapters.<adapter_type>.available is required and must be literal bool
report.adapters.<adapter_type>.available must be literal bool; got str
```

### 6.3 `adapters` mapping contract

If a non-blocked report does not have a mapping `adapters` field:

```text
allowed = false
reason = "adapter_readiness_report_schema_invalid"
error = "adapter_readiness_report_schema_invalid"
errors includes "report.adapters must be a mapping"
```

Do not collapse malformed `adapters` to `{}`.

### 6.4 Adapter entry contract

If `adapters` is a mapping but the manifest adapter key is absent:

```text
reason = "adapter_readiness_adapter_missing"
```

Preserve this existing behavior for a genuinely missing adapter entry.

If the adapter key exists but its value is not a mapping:

```text
reason = "adapter_readiness_report_schema_invalid"
errors includes "report.adapters.<adapter_type> must be a mapping"
```

This distinguishes a missing adapter from a malformed adapter document.

### 6.5 `adapter_doc.available` literal-bool contract

If `adapter_doc.available` is missing or not a literal `bool`:

```text
reason = "adapter_readiness_report_schema_invalid"
error = "adapter_readiness_report_schema_invalid"
errors includes "report.adapters.<adapter_type>.available is required and must be literal bool"
```

If `adapter_doc.available is False`:

```text
reason = "adapter_binary_missing"
```

Preserve existing payload fields:

```text
adapter_type
reported_readiness
reported_resolved_path
started_real_process=false
started_adapter_process=false
```

If `adapter_doc.available is True`:

Continue existing checks:

```text
current_adapter_binary_missing
adapter_binary_path_changed
codex_automation_lock_mismatch
allowed gate pass
```

## 7. Standard blocked/error payload contract

For every D5.3.15 planned blocked result, preserve the base execution-safety payload:

```text
phase = "B13-adapter-readiness-gate-binding"
gate_enabled = true
required_modes = <required modes>
execution_mode = <mode>
agent = <agent>
adapter_type = <adapter type>
manifest_adapter_type = <manifest adapter type>
configured_adapter_type = <configured adapter type>
started_real_process = false
started_adapter_process = false
auto_install = false
auto_login = false
read_tokens = false
```

When a report exists, also preserve:

```text
readiness_report_path
readiness_event_id
readiness_created_at
```

For report-contract failures, include:

```text
reason
error
errors
```

This avoids converting report-level/config-level/schema-level failures into generic adapter lookup failures.

## 8. Planned future RED tests, not to be added now

The following tests must wait for separate explicit implementation authorization. They are recorded here only so CodeX can review the contract.

Future test file:

```text
tests/aiwg/runners/test_d5315_adapter_readiness_gate_report_interpretation_schema.py
```

### Test 1 - blocked report wins before adapter lookup

Setup:

1. Create valid gate config, DB, task, and manifest.
2. Write a latest readiness event pointing to a report with:

```json
{
  "schema_version": "aiwg.adapter_binary_readiness.v1",
  "status": "blocked",
  "error": "config_contract_invalid",
  "errors": ["config_contract_invalid: example"],
  "adapters": {}
}
```

Expected:

```text
result.allowed is False
result.reason == "config_contract_invalid"
result.payload.error == "config_contract_invalid"
result.payload.errors includes "config_contract_invalid: example"
result.payload.started_real_process is False
result.payload.started_adapter_process is False
```

It must not return `adapter_readiness_adapter_missing`.

### Test 2 - report error wins even when status is not blocked

Setup report:

```json
{
  "schema_version": "aiwg.adapter_binary_readiness.v1",
  "status": "checked",
  "error": "adapter_readiness_report_error",
  "errors": ["example report error"],
  "adapters": {}
}
```

Expected:

```text
reason == "adapter_readiness_report_error"
error == "adapter_readiness_report_error"
errors includes "example report error"
```

It must not proceed to adapter lookup.

### Test 3 - non-blocked malformed adapters shape fails as report-schema invalid

Setup report:

```json
{
  "schema_version": "aiwg.adapter_binary_readiness.v1",
  "adapters": []
}
```

Expected:

```text
reason == "adapter_readiness_report_schema_invalid"
error == "adapter_readiness_report_schema_invalid"
errors includes "report.adapters must be a mapping"
```

It must not return `adapter_readiness_adapter_missing`.

### Test 4 - adapter key absent still preserves adapter-missing semantics

Setup report:

```json
{
  "schema_version": "aiwg.adapter_binary_readiness.v1",
  "adapters": {}
}
```

Expected:

```text
reason == "adapter_readiness_adapter_missing"
```

This preserves the existing genuine adapter-missing signal.

### Test 5 - adapter entry present but malformed fails as report-schema invalid

Setup report:

```json
{
  "schema_version": "aiwg.adapter_binary_readiness.v1",
  "adapters": {
    "opencode": []
  }
}
```

Expected:

```text
reason == "adapter_readiness_report_schema_invalid"
errors includes "report.adapters.opencode must be a mapping"
```

### Test 6 - non-literal available value cannot pass through truthiness

Parameterized malformed values:

```text
"false"
"true"
0
1
null
[]
{}
```

Expected for each:

```text
reason == "adapter_readiness_report_schema_invalid"
error == "adapter_readiness_report_schema_invalid"
errors includes "report.adapters.opencode.available must be literal bool"
started_real_process is False
started_adapter_process is False
```

This is the direct successor to D5.3.14's `enabled` literal-bool cleanup, but for persisted readiness reports rather than config.

### Test 7 - literal available=false preserves adapter_binary_missing

Setup report:

```json
{
  "schema_version": "aiwg.adapter_binary_readiness.v1",
  "adapters": {
    "opencode": {
      "available": false,
      "readiness": "missing",
      "resolved_path": null
    }
  }
}
```

Expected:

```text
reason == "adapter_binary_missing"
reported_readiness == "missing"
reported_resolved_path is None
```

### Test 8 - resume_preflight surfaces report-schema invalid without running agents

Use the same malformed `available="false"` report through `resume_preflight(...)`.

Expected:

```text
result.status == "adapter_readiness_blocked"
result.error == "adapter_readiness_report_schema_invalid"
latest adapter_readiness_gate_blocked payload.reason == "adapter_readiness_report_schema_invalid"
agent_runs count does not increase
operator approval is not consumed
```

### Test 9 - approve_real_start surfaces report-schema invalid without authorization artifact

Use a plan/probe chain and then make the latest readiness report malformed.

Expected:

```text
result.status == "adapter_readiness_blocked"
result.error == "adapter_readiness_report_schema_invalid"
result.authorization_path is None
agent_runs count does not increase
latest adapter_readiness_gate_blocked payload.started_real_process is False
latest payload.started_adapter_process is False
```

## 9. Future implementation guidance after review authorization

Do not execute these steps during planning. If CodeX passes this plan and the user explicitly authorizes implementation:

1. Add the future D5.3.15 test file first.
2. Run the D5.3.15 targeted tests and confirm intended RED failures.
3. Add a small report-normalization helper in `aiwg/adapter_readiness_gate.py` only.
4. Move report-level blocked/error interpretation before adapter lookup and before any report shape normalization.
5. Replace silent `adapters` fallback with explicit schema invalidation for non-blocked reports.
6. Split missing adapter entry from malformed adapter entry.
7. Replace `bool(adapter_doc.get("available", False))` with literal-bool validation.
8. Preserve existing behavior for `available=false` and `available=true` happy/current-path paths.
9. Run targeted D5.3.15 tests.
10. Run D5.3.13 + D5.3.14 regressions.
11. Run B13 gate binding regressions.
12. Run full AIWG suite only after GREEN.
13. Write implementation acceptance as `completed_ready_for_codex_review`; leave `codex_review` pending.

## 10. Non-goals for D5.3.15

D5.3.15 should not include:

- new implementation during planning;
- RED tests during planning;
- new report writer format in `adapter_binary_readiness.py`;
- changes to `aiwg.yaml`;
- config-validator changes in `aiwg/config.py`;
- `_required_modes(...)` validation;
- `_max_age_minutes(...)` validation;
- Codex desktop automation bool normalization;
- real process execution;
- MCP mutation tools;
- protected business repository writes;
- GitHub PR mutation/comments;
- CodeX Automation changes.

## 11. Planning acceptance criteria

This D5.3.15 planning-only phase is ready for CodeX review when:

1. This plan exists under `docs/plans/`.
2. Planning acceptance exists under `docs/ai-workgroup/state/artifacts/phase-d5-3-15-adapter-readiness-gate-report-interpretation-schema-normalization-planning/acceptance.json`.
3. Acceptance status is `completed_ready_for_codex_review`.
4. `implementation_started=false`.
5. `red_tests_added=false`.
6. `codex_review.status=pending` and `codex_review.passed=null`.
7. Static reconnaissance records the current interpretation order and risk points.
8. `doctor` remains OK.
9. MCP `--list-tools` remains read-only: `status`, `list_tasks`, `get_task`, `recent_events`.
10. Protected AIVideoTrans marker scan for D5.3.15 terms has `0 hits`.
11. Git diff contains only the planning doc as a tracked repository change; local acceptance remains ignored state.
12. No runtime/test/config/business-repo files are changed.

## 12. CodeX quick review focus

Ask CodeX to review these contract choices before implementation:

1. Is `adapter_readiness_gate report interpretation / schema-normalization` the right D5.3.15 planning slice after D5.3.14?
2. Should report-level `status=blocked` or non-empty `error` always win before adapter lookup?
3. Is `adapter_readiness_report_schema_invalid` the right standard reason for malformed non-blocked report content?
4. Should malformed `adapters` shape be schema-invalid instead of adapter-missing?
5. Should an absent adapter key preserve `adapter_readiness_adapter_missing`?
6. Should an existing but non-mapping adapter entry be schema-invalid?
7. Should `adapter_doc.available` be required literal `bool`, with non-literal values schema-invalid and literal `false` preserving `adapter_binary_missing`?
8. Should Codex `desktop_automation_allowed` bool cleanup remain a separate future slice?
9. Confirm no implementation or RED tests should begin until CodeX/Human authorization after planning review.

## 13. Recommended next

Submit this D5.3.15 planning-only artifact to CodeX quick review. Stop after planning acceptance is written and read-only verification passes.
