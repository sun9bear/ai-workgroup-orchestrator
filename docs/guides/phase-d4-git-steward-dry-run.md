# Phase D4 Git Steward Dry-Run Worktree / Commit / PR Gate

## Status

Implemented as a D4 fake/preflight minimum slice. This phase remains dry-run-only and does not open real agents, protected business-repository writes, Git mutation, or MCP mutation tools.

```text
ready_for_real_agent_execution=false
ready_for_protected_business_repository_write=false
mcp_mutation_tools_exposed=false
allow_write=false
allow_push=false
allow_merge=false
target_writes_performed=false
git_commit_performed=false
git_push_performed=false
git_merge_performed=false
```

## Scope

D4 adds a deterministic Git Steward preflight layer that makes branch/worktree/commit/PR decisions explicit before any Git mutation can be considered.

Implemented surfaces:

- SQLite schema migration `phase_d4_git_steward_dry_run` (`SCHEMA_VERSION=5`).
- Tables:
  - `git_worktree_proposals`
  - `git_commit_proposals`
  - `pr_gate_status`
- Python API:
  - `aiwg.git_steward.plan_git_dry_run(...)`
  - `aiwg.git_steward.get_pr_gate_status(...)`
- CLI:
  - `python -m aiwg.cli git-plan --dry-run ...`
  - `python -m aiwg.cli pr-gate-status --json ...`

## Safety boundary

D4 deliberately does **not** open any of the following:

- real agent execution
- real adapter process dispatch
- MCP mutation tools
- protected business repository writes
- Git commit / push / merge / PR mutation
- deploy
- secret access
- CodeX Automation modification

Every D4 Git Steward result and artifact records:

```json
{
  "dry_run": true,
  "target_writes_performed": false,
  "git_commit_performed": false,
  "git_push_performed": false,
  "git_merge_performed": false,
  "mcp_mutation_tools_exposed": false
}
```

`target_root` is audit context only. The reported `worktree_path` is a proposal string, not a created directory. D4 writes mutable state only under the Orchestrator root:

```text
<orchestrator_root>/docs/ai-workgroup/state/tasks.sqlite
<orchestrator_root>/docs/ai-workgroup/state/artifacts/phase-d4-git-steward/git-plan-<plan_id>.json
```

D4 validates these mutable paths before writing:

- `state_db` must resolve under `<orchestrator_root>/docs/ai-workgroup/state` and must not overlap the supplied `target_root`.
- `artifact_root` must resolve under `<orchestrator_root>/docs/ai-workgroup/state/artifacts` and must not overlap the supplied `target_root`.
- changed file paths are relative POSIX paths only; absolute paths, `..`, drive-relative paths such as `C:foo`, and any colon in a path segment are rejected before SQLite/artifact writes.

## Gate behavior

`git-plan --dry-run` performs only deterministic planning:

1. Normalize and validate changed file paths.
2. Validate Orchestrator-only `state_db` and `artifact_root`.
3. Fail closed if any mutation switches are enabled, including policy `allow_push=true`, `allow_merge=true`, `allow_write=true`, or `git.allow_auto_commit=true`.
4. Exclude control-plane/runtime paths from commit proposals:
   - `.codegraph/`
   - `.codex_worktrees/`
   - `docs/ai-workgroup/state/`
5. Classify the remaining files into a single scope such as `apf_frontend` or `apf_backend`.
6. Deny mixed APF frontend/backend commit proposals.
7. Record a worktree proposal, commit proposal, and read-only PR gate status in SQLite.
8. Write a dry-run plan artifact under the Orchestrator artifact tree.

Current dry-run gate states include:

- `planned`
- `policy_denied`
- `scope_mixed_denied`
- `scope_mismatch_denied`
- `no_candidate_changes`
- `pr_not_created_dry_run`
- `not_found` for read-only status lookups with no row

## CLI examples

Plan a frontend-only APF candidate without target writes:

```bash
python -m aiwg.cli git-plan \
  --config aiwg.yaml \
  --plan-id D4-aivideotrans-apf-frontend-preflight-001 \
  --task-id APF3b-frontend \
  --target-root D:/example/protected-business-repo \
  --scope apf_frontend \
  --changed-file frontend-next/src/components/marketing/anonymous-trial-launcher.tsx \
  --changed-file .codegraph/cache.json \
  --changed-file .codex_worktrees/tmp/generated.patch \
  --changed-file docs/ai-workgroup/state/tasks.sqlite \
  --dry-run \
  --json
```

Read the PR gate status without creating or mutating a PR:

```bash
python -m aiwg.cli pr-gate-status \
  --config aiwg.yaml \
  --plan-id D4-aivideotrans-apf-frontend-preflight-001 \
  --json
```

`git-plan` refuses to run without `--dry-run`.

## Tests

D4 targeted tests are in:

```text
tests/aiwg/git/test_d4_git_steward_dry_run.py
```

They cover:

- schema migration `SCHEMA_VERSION=5` installs Git Steward tables;
- dry-run planning records worktree, commit, PR-gate status, and Orchestrator-only artifact;
- `.codegraph/`, `.codex_worktrees/`, and `docs/ai-workgroup/state/` files are excluded from commit proposals;
- APF frontend and APF backend files cannot be mixed into one commit proposal;
- mutation switches such as `allow_push`, `allow_merge`, and `git.allow_auto_commit` fail closed;
- unsafe changed paths (`C:foo`, absolute paths, `..`, colon segments, `.`, `./`, and ASCII control characters including leading/trailing control characters) are rejected before writes;
- `git-plan` requires `--dry-run`;
- `pr-gate-status` is read-only and reports no mutation actions.

## D4.1 hardening

CodeX accepted the D4 dry-run posture but requested three pre-D5 hardening fixes. D4.1 implements them without enabling real Git mutation or target writes:

1. **Final artifact / SQLite consistency**
   - Final `git-plan-*.json` is no longer written before SQLite statements run.
   - The artifact is finalized only after the SQLite proposal transaction succeeds.
   - Regression test forces a SQLite failure and proves no final `git-plan-*.json` is left behind.
2. **Changed-path hardening**
   - D4.1 now rejects `.` / `./` and ASCII control characters in changed paths.
   - Existing rejects for drive-relative `C:foo`, absolute paths, parent traversal, and colon segments remain in place.
3. **Reused `plan_id` conflict hardening**
   - A reused `plan_id` with conflicting task, target root, scope, branch, or proposed worktree identity now returns `plan_id_conflict_denied`.
   - The existing proposal rows and existing artifact are preserved, avoiding semantic mixing in audit records.

## Latest verification

```text
Initial D4.1 RED: 5 expected failures before implementation (`.`, `./`, control character path, orphan final artifact on SQLite failure, reused plan_id conflict)
Independent-review blocker RED: 3 expected failures for leading/trailing ASCII control characters before post-review fix (`\n`, `\t`, `\x1f`)
D4.1 targeted: 17 passed in 0.92s
D4.1/D3/state/operator regression slice: 29 passed in 3.67s
full suite: 251 passed in 44.37s
CLI dry-run sample: planned; target_writes_performed=false; git_commit_performed=false; git_push_performed=false; git_merge_performed=false; mcp_mutation_tools_exposed=false; pr-gate-status read_only=true and mutation_actions=[]
doctor: AIWG doctor: OK; non-blocking warning remains because orchestrator is not a git repository
MCP tools: status,list_tasks,get_task,recent_events only; tool_count=4; mutation_marker_tools=
AIVideoTrans D4 artifact scan: d4_specific_worktree_path_exists=False; business_d4_artifacts=0; business_git_plan_artifacts=0; business_pr_gate_artifacts=0; business_tasks_sqlite=0
```

The AIVideoTrans repository already has unrelated `.codex_worktrees` children from earlier worktrees. D4/D4.1 did not create the proposed `d4-aivideotrans-apf-frontend-preflight-001` worktree path and did not write D4 artifacts or SQLite state into the business repository.

## Independent review

D4 original independent post-implementation safety review passed before CodeX D4.1 hardening feedback. The first D4.1 independent review then failed closed on one additional boundary case: `_normalize_changed_path()` checked control characters after `strip()`, so leading/trailing control characters could be silently removed. D4.1 post-review hardening added RED cases for trailing newline, leading tab, and trailing unit separator, then moved ASCII control-character detection before trimming.

Post-review verification now includes:

```text
blocker RED: 3 expected failures before fix
post-review unsafe-path slice: 10 passed in 0.11s
D4.1 targeted: 17 passed in 0.92s
D4.1/D3/state/operator regression slice: 29 passed in 3.67s
full suite: 251 passed in 44.37s
```

Final independent post-fix review passed:

```text
passed=true
security_concerns=[]
logic_errors=[]
safety_issues=[]
docs_issues=[]
reviewer targeted: 17 passed in 0.93s
reviewer regression slice: 29 passed in 3.57s
reviewer full suite: 251 passed in 44.65s
reviewer doctor: AIWG doctor OK; only non-blocking not-a-git-repository warning
reviewer MCP: status,list_tasks,get_task,recent_events only; tool_count=4; mutation_marker_tools=[]
reviewer AIVideoTrans boundary: d4_specific_worktree_path_exists=false; D4 artifacts=0; git-plan artifacts=0; pr-gate artifacts=0; tasks.sqlite=0
reviewer control-char probe: trailing newline, leading tab, and trailing unit separator all raise unsafe_changed_path with no durable side effects
```

D4.1 is marked `completed_ready_for_codex_review` in the acceptance artifact.

## Remaining constraints before later phases

D4 is **not** approval to execute real agents or protected writes. Later phases must still keep defaults fail-closed unless the user explicitly authorizes real execution:

```text
allow_write=false
allow_real_agents=false
allow_push=false
allow_merge=false
allow_deploy=false
mcp_mutation_tools_exposed=false
```
