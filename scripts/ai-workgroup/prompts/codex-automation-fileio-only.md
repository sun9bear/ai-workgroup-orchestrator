You are CodeX running the AI workgroup Phase 0 CO1 file I/O-only automation smoke test.

Workspace:

```text
D:\AIGroup\ai-workgroup-orchestrator
```

Task file:

```text
docs/ai-workgroup/inbox/CodeX/2026-05-27T151000_from-Human_to-CodeX_type-instruction_task-CO1_codex-fileio-only.md
```

Expected report path:

```text
docs/ai-workgroup/inbox/Human/2026-05-27T152000_from-CodeX_to-Human_type-report_task-CO1_codex-fileio-only.md
```

Goal:

- Verify whether Codex Automation can use non-shell file I/O in this workspace.
- Do not run shell commands.
- Do not run PowerShell, Python, Node, package managers, or validation scripts.
- Use only built-in file read/write capabilities if they are available.

Rules:

- The only expected file mutation is the report under `docs/ai-workgroup/inbox/Human/`.
- Do not edit source code, tests, project configuration, deployment files, secrets, migrations, or `docs/ai-workgroup/state/**`.
- If file read is unavailable, still write a `needs_clarification` report based on this prompt and explicitly say that non-shell file read was unavailable.
- Default report language is Chinese. Write summaries, decisions, risks, validation notes, and next steps in Chinese. Keep file paths, commands, front matter keys, status values, API names, and code symbols in their original English.

Expected report front matter:

```yaml
---
id: CO1-msg-002
task: CO1-codex-fileio-only
from: CodeX
to: Human
type: report
status: reported
priority: medium
reply_to: CO1-msg-001
requires_human: false
created_at: 2026-05-27T15:20:00+08:00
can_write: false
allowed_files: []
forbidden_files:
  - .env
  - migrations/**
attempt: 0
max_attempts: 1
timeout_minutes: 30
---
```

If non-shell file read fails, use `status: needs_clarification` instead of `reported`.

The report body must include these sections:

```text
# Report

## Summary

## Validation

## Risks
```
