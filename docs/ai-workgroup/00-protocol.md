# AI Workgroup Protocol

This protocol defines the local file queue used by AI coding runners during Phase 0.

## Goals

- Keep task ownership and state auditable.
- Validate messages before any runner consumes them.
- Prevent write scope drift with `can_write`, `allowed_files`, and `forbidden_files`.
- Keep Human Gate decisions outside unattended automation.

## Directories

```text
docs/ai-workgroup/
  inbox/<agent>/
  working/<agent>/
  done/
  archive/
  shared/
  state/
    locks/
    events.<agent>.jsonl
```

## Required Message Fields

Every message starts with YAML front matter:

```yaml
---
id: T0-msg-001
task: T0
from: CodeX
to: Fake
type: instruction
status: ready
priority: medium
reply_to: ""
requires_human: false
created_at: 2026-05-27T12:00:00+08:00
can_write: false
context_files: []
allowed_files: []
forbidden_files:
  - .env
attempt: 0
max_attempts: 2
timeout_minutes: 30
---
```

## State Rules

- `ready`: waiting in `inbox/<agent>/`.
- `claimed`: runner has claimed the task and moved it to `working/<agent>/`.
- `working`: runner is actively processing the task.
- `reported`: runner wrote a report to `inbox/CodeX/`.
- `reviewing`: CodeX or a delegated reviewer is checking the report.
- `needs_revision`: reviewer rejected the result and asks for changes.
- `needs_review`: result needs manual or CodeX review before proceeding.
- `needs_clarification`: runner cannot safely execute the task; default flow is a new reply to `inbox/CodeX/`.
- `waiting_human`: task is blocked on Human Gate input.
- `waiting_codex`: task is blocked on CodeX orchestration or review.
- `review_degraded`: CodeX review is unavailable and the pre-authorized `review_delegate` is active.
- `stale_claim`: watcher/scanner found a task or lock that appears abandoned.
- `needs_manual_recovery`: diff, lock, or state is unsafe for automatic recovery; Human must inspect.
- `approved`: reviewer approved the result.
- `done`: task has been closed.
- `cancelled`: Human or CodeX cancelled the task.
- `failed`: runner failed before producing a valid report.
- `archived`: message is retained for audit only.

`needs_manual_recovery` exits only after Human inspection: `ready`, `needs_revision`, `cancelled`, or `done`. CodeX may recommend recovery, but must not automatically restore write-capable tasks without explicit approval.

`requires_human: true` is a hard automation stop. Scanner and runner policy must skip it; a runner must not process it unattended.

## Scope Rules

- If `can_write: false`, `allowed_files` must be empty.
- Use `context_files` for read-only guidance.
- `forbidden_files` always wins over `allowed_files`.
- Human Gate tasks must use `requires_human: true`.

## Language Rules

- Default report and documentation language is Chinese.
- Human reports, CodeX review conclusions, task-splitting notes, decision records, and design/spec documents should use Chinese for summaries, decisions, risks, validation notes, and next steps.
- Keep code symbols, file paths, commands, front matter keys, status values, API names, package names, and product identifiers in their original English.
- If a task explicitly asks for another language, use that language only for the requested user-facing copy; still include a Chinese summary when writing protocol reports for Human or CodeX.
