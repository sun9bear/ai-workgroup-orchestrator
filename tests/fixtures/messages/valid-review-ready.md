---
id: T0-msg-001
task: T0
from: CodeX
to: OpenCode
type: review
status: ready
priority: medium
reply_to: ""
requires_human: false
created_at: 2026-05-27T11:45:00+08:00
can_write: false
context_files:
  - docs/plans/2026-05-25-ai-agent-collaboration-orchestration-plan.md
allowed_files: []
forbidden_files:
  - .env
  - migrations/**
acceptance:
  - powershell -ExecutionPolicy Bypass -File scripts/ai-workgroup/validate-message.ps1 -Path tests/fixtures/messages/valid-review-ready.md
claimed_by: ""
claimed_at: ""
lock_id: ""
attempt: 0
max_attempts: 2
timeout_minutes: 30
review_delegate: Claude-Code
---

# Review Fixture

## Request
- Validate this review-only message fixture.

## Constraints
- Do not modify files.
