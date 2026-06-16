---
id: T0-msg-002
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
allowed_files:
  - docs/**
forbidden_files:
  - .env
attempt: 0
max_attempts: 2
timeout_minutes: 30
---

# Invalid Fixture

`can_write: false` must not grant `allowed_files`.
