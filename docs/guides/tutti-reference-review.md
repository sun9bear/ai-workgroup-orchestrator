# Tutti reference review for AI Workgroup Orchestrator

Date: 2026-06-06

Reference source:

- Local checkout: `D:\AIGroup\_external\tutti`
- Upstream: `https://github.com/nutthouse/tutti`
- License: MIT

## Copyright and reuse boundary

Tutti is MIT licensed. That license permits use, modification, redistribution, and sublicensing, provided the MIT copyright notice is preserved in copied/substantial portions.

For this project, the safer default is:

- Learn architecture and operational patterns.
- Do not copy Tutti source code into AIWG unless a later change explicitly records the copied file/function and adds the MIT notice.
- If any substantial Tutti code is imported, add a third-party notice entry and keep the copied portion isolated.
- Prefer independent Python implementations that match our safety model instead of Rust/tmux-specific implementation details.

## High-value patterns to absorb

### 1. Agent operations as versioned config

Tutti treats team topology, runtime, scope, workflows, hooks, gates, and budgets as versioned config (`tutti.toml`).

AIWG should keep this idea, but use the existing `aiwg.yaml` / SQLite control plane:

- Define logical roles first: Tech Lead, Implementer, Reviewer, Git Steward, Advisor.
- Map roles to adapters separately: Claude Code CLI, Codex CLI/API, Hermes bridge, Fake.
- Keep per-role scope and permissions in config, not in ad hoc prompts.
- Record config snapshot hash on every run.

### 2. Machine-readable state contract

Tutti exposes stable state files for agents and health. The important idea is not the file format; it is the stable machine contract.

AIWG should add or harden these SQLite-backed surfaces:

- `agent_states`: role, adapter, status, last_seen_at, current_task_id, worktree_path, branch, auth/rate/provider health.
- `workflow_runs`: run_id, workflow_name, phase, status, current_step, resume_eligible.
- `workflow_step_outputs`: step_id, output path, summary, verifier status.
- Dashboard and MCP read-only tools should consume these tables rather than parsing Markdown or terminal text.

### 3. Deterministic workflow checkpoints

Tutti stores workflow intent, step output, checkpoint, and resume records.

AIWG should adopt this before real agent execution:

- Every dispatch creates a step intent record before execution.
- Every agent result is linked to the intent by run_id / step_id.
- A failed workflow can resume from the last successful deterministic gate, not from a guessed inbox state.
- Re-dispatch must require idempotency_key and prior outcome classification.

### 4. Runtime detection and health classification

Tutti classifies agent runtime output into auth failure, rate limit, provider down, working, idle, and completion states.

AIWG should adopt the classification model, but not copy pattern lists blindly:

- `auth_failed`
- `rate_limited`
- `provider_down`
- `working`
- `idle`
- `completed`
- `unknown`

Each adapter should produce structured health/status events. The dashboard should show these states directly, so Human can see whether a role is actually working or only has a stale heartbeat.

### 5. Worktree isolation as the default for write-capable work

Tutti gives agents isolated worktrees and deterministic branches.

AIWG should make this a Git Steward-controlled requirement before protected writes:

- No write-capable task on `main`.
- Each implementation run gets a deterministic branch/worktree.
- Git Steward owns branch creation, commit proposal, push proposal, PR proposal, and CI/review follow-up.
- Implementer only writes within the assigned worktree and allowed paths.
- Dashboard should show branch/worktree per active task.

### 6. Generic PR review loop

Tutti separates the durable gate from the review adapter: review feedback resolved + required checks green.

AIWG should model GitHub review as a generic gate:

- `pr_opened`
- `ci_pending`
- `ci_failed`
- `review_requested`
- `review_changes_requested`
- `review_approved`
- `review_threads_unresolved`
- `ready_for_merge_proposal`

GitHub/Codex review/CodeRabbit/human review are adapters feeding the same gate, not separate orchestration logic.

### 7. Budget and permission preflight

Tutti runs pre-exec budget and permission checks.

AIWG already has safety switches and write gates. D3/D4 should add:

- Per-run and per-role budget snapshots.
- Warn/enforce budget modes.
- Adapter command preflight records.
- Explicit event when a dispatch is denied by budget, network, secret, destructive command, or protected repo boundary.

### 8. Dashboard as control room, not just inbox

Tutti's dashboard direction is useful: show flow, bottlenecks, role state, logs, and handoffs.

AIWG dashboard should evolve from message list to control-room view:

- Current phase and active workflow run.
- Role health cards.
- Queue by role.
- Blockers and stale states first.
- Git/PR/CI gate status.
- Recent deterministic events.
- Human Gate only when the phase envelope truly requires Human.

## Patterns not to absorb directly

- Do not switch AIWG to Rust just because Tutti is Rust.
- Do not adopt tmux as a Windows-first runtime dependency.
- Do not make Desktop app sessions the reliable execution substrate.
- Do not expose mutating HTTP/MCP actions before write-gate and approval envelope hardening.
- Do not copy Tutti's runtime pattern strings blindly; build adapter-specific tests from our own Windows/Claude/Codex outputs.
- Do not let workflow config bypass AIWG's safety switches, write gate, or protected repository boundary checks.

## Recommended AIWG backlog after D2.2

1. D3 preflight workflow ledger:
   - Add workflow_run / workflow_step / step_intent / step_output schema.
   - Add fake workflow execution with checkpoints and resume.
   - Keep target writes disabled.

2. D3 role health contract:
   - Add agent_state / agent_health tables.
   - Implement adapter status classifications: auth, rate limit, provider down, working, idle, completed, unknown.
   - Dashboard should show stale role heartbeat separately from real runner activity.

3. D4 worktree and Git Steward dry-run:
   - Model branch/worktree proposal in SQLite.
   - Git Steward can propose branch/commit/PR actions but not push/merge by default.
   - Add tests proving frontend/APF/backend scopes cannot be mixed.

4. D4 PR gate adapter design:
   - Add generic PR gate states.
   - GitHub/Codex review monitor becomes one adapter feeding those states.
   - No auto-merge.

5. D5 controlled mutation tools:
   - Only after write-gate, approval envelope, workflow ledger, role health, and Git Steward dry-run are green.
   - MCP mutation tools remain disabled until this phase.

## Bottom line

Tutti validates that AIWG's direction is reasonable: local control plane, versioned agent ops, stable state, worktree isolation, gates, dashboard, and PR/review loop.

The main difference is safety posture. Tutti is optimized for running agent teams; AIWG should remain optimized for deterministic gates around protected business repositories. We should borrow Tutti's operational shapes, not replace AIWG with Tutti.
