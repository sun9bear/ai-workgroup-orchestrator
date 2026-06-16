# AIWG Tutti reference absorption plan

Date: 2026-06-06

Reference review:

- `docs/guides/tutti-reference-review.md`

## Decision

Use Tutti as a reference implementation for agent-operations patterns, not as a runtime dependency.

Reasons:

- Tutti's MIT license allows reuse, but copying code would add attribution and maintenance obligations.
- Tutti is Rust/tmux-oriented; AIWG is Python/SQLite/MCP and Windows-first.
- AIWG's central requirement is protected-repository safety, dry-run gates, and deterministic auditability before real agents.
- Tutti is most valuable as evidence for which operational surfaces are worth building.

## Absorption scope

Allowed:

- Architecture ideas.
- State-machine shapes.
- Config and workflow concepts.
- Dashboard information hierarchy.
- PR/review gate model.
- Runtime health classification taxonomy.

Not allowed without explicit later approval:

- Copying Tutti Rust source code.
- Adding Tutti as a required dependency.
- Adding tmux as a Windows runtime requirement.
- Enabling real agent execution, protected repository writes, push, merge, deploy, or MCP mutation tools.

## D3 candidate: workflow ledger and checkpoint preflight

Status: implemented as the D3-preflight minimum slice. See `docs/guides/phase-d3-workflow-preflight.md` and `docs/ai-workgroup/state/artifacts/phase-d3-workflow-preflight/acceptance.json`.

Goal: make AIWG workflows resumable and auditable without guessing from inbox files.

Deliverables:

- SQLite tables:
  - `workflow_runs`
  - `workflow_steps`
  - `workflow_step_intents`
  - `workflow_step_outputs`
- CLI:
  - `python -m aiwg.cli workflow-plan --dry-run`
  - `python -m aiwg.cli workflow-status --json`
- Tests:
  - intent is written before execution
  - fake step output links to intent
  - failed step can resume from last successful gate
  - duplicate idempotency key does not re-dispatch

Constraints:

- Fake adapter only.
- No protected business repository writes.
- No MCP mutation tools.

## D3 candidate: role health contract

Goal: distinguish real runner activity from stale Desktop heartbeat or stale queue state.

Deliverables:

- SQLite tables:
  - `agent_states`
  - `agent_health_events`
- Status taxonomy:
  - `auth_failed`
  - `rate_limited`
  - `provider_down`
  - `working`
  - `idle`
  - `completed`
  - `unknown`
- Dashboard:
  - role cards show last_seen_at, current_task_id, adapter status, and stale age
  - active task count must come from claimed/working task state, not merely ready message presence
- Tests with local fixture outputs from Claude Code/Codex/Hermes where available.

Constraints:

- Detection may parse adapter logs, but output must be normalized before entering SQLite.
- Do not depend on Codex Desktop or Claude Desktop GUI session wakeup as the reliable backend.

## D4 candidate: Git Steward dry-run worktree model

Status: implemented as the D4 dry-run minimum slice. See `docs/guides/phase-d4-git-steward-dry-run.md` and `docs/ai-workgroup/state/artifacts/phase-d4-git-steward/acceptance.json`.

Goal: make branch/worktree/commit/PR decisions explicit and testable before allowing Git writes.

Deliverables:

- SQLite tables:
  - `git_worktree_proposals`
  - `git_commit_proposals`
  - `pr_gate_status`
- CLI:
  - `python -m aiwg.cli git-plan --dry-run`
  - `python -m aiwg.cli pr-gate-status --json`
- Gate states:
  - Implemented D4 minimum slice:
    - `planned`
    - `policy_denied`
    - `scope_mixed_denied`
    - `scope_mismatch_denied`
    - `no_candidate_changes`
    - `pr_not_created_dry_run`
  - Future/reference PR and CI states, not opened by D4:
    - `branch_proposed`
    - `commit_proposed`
    - `pr_proposed`
    - `ci_pending`
    - `ci_failed`
    - `review_changes_requested`
    - `review_approved`
    - `review_threads_unresolved`
    - `ready_for_merge_proposal`
- Tests:
  - APF frontend and APF backend scopes cannot be mixed into one commit proposal
  - `.codegraph/`, `.codex_worktrees/`, and AIWG state files are excluded
  - no push/merge occurs while `allow_push=false` / `allow_merge=false`

Constraints:

- Dry-run only.
- No auto-push.
- No auto-merge.

## D4 candidate: generic external review gate

Goal: avoid hard-coding any one review provider.

Deliverables:

- Review adapter interface:
  - `poll_review_status`
  - `collect_actionable_feedback`
  - `classify_review_gate`
- Supported sources can later include:
  - GitHub reviews
  - Codex review
  - CodeRabbit
  - Human review reports
- The durable gate is:
  - required checks green
  - actionable review feedback resolved
  - no unresolved review threads

Constraints:

- Read-only GitHub/PR inspection first.
- No PR mutation until a later approval envelope allows it.

## D5 candidate: controlled mutation surface

Do not begin D5 until D3 and D4 are green.

Minimum prerequisites:

- write gate remains fail-closed
- approval envelope schema hardened
- workflow ledger and resume tested
- role health contract active
- Git Steward dry-run tested
- external review gate read-only tested
- dashboard shows safe/blocked/active states accurately

Only then consider:

- MCP mutation tools
- real agent dispatch
- protected repository writes

Even in D5, default remains:

- `allow_write=false`
- `allow_push=false`
- `allow_merge=false`
- `allow_deploy=false`

## License note

Tutti is MIT licensed. If a future change copies substantial Tutti code, that change must preserve the MIT notice and document copied files/functions. This plan intentionally imports no Tutti source code.
