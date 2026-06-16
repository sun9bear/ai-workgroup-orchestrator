param(
    [string] $ProjectRoot = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
Set-Location $resolvedRoot

$tmpRoot = Join-Path $resolvedRoot 'tests/tmp/orphan-lock'
if ((Test-Path -LiteralPath $tmpRoot) -and -not ((Resolve-Path -LiteralPath $tmpRoot).ProviderPath.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase))) {
    throw "Refusing to clean temp path outside project: $tmpRoot"
}
Remove-Item -LiteralPath $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue

$workgroupRoot = Join-Path $tmpRoot 'docs/ai-workgroup'
$fakeInbox = Join-Path $workgroupRoot 'inbox/Fake'
$locks = Join-Path $workgroupRoot 'state/locks'
New-Item -ItemType Directory -Force -Path $fakeInbox, $locks | Out-Null

$messageId = 'OL0-msg-001'
$messagePath = Join-Path $fakeInbox '2026-05-27T211000_from-CodeX_to-Fake_type-instruction_task-OL0_orphan-lock.md'
$message = @"
---
id: $messageId
task: OL0-orphan-lock
from: CodeX
to: Fake
type: instruction
status: ready
priority: high
reply_to: ""
requires_human: false
created_at: 2026-05-27T21:10:00+08:00
can_write: false
context_files: []
allowed_files: []
forbidden_files:
  - .env
attempt: 0
max_attempts: 1
timeout_minutes: 5
---

# Orphan Lock Smoke

This task simulates a crash after lock creation but before moving to working/.
"@
[System.IO.File]::WriteAllText($messagePath, $message, [System.Text.UTF8Encoding]::new($false))

$lockPath = Join-Path $locks "$messageId.lock"
$oldCreatedAt = (Get-Date).AddMinutes(-30).ToString('yyyy-MM-ddTHH:mm:ssK')
$lockPayload = [ordered]@{
    message_id = $messageId
    agent = 'Fake'
    runner_id = 'smoke-test'
    created_at = $oldCreatedAt
    source_path = (Resolve-Path -LiteralPath $messagePath).ProviderPath
} | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText($lockPath, $lockPayload, [System.Text.UTF8Encoding]::new($false))

$output = & 'scripts/ai-workgroup/check-stale-claims.ps1' -WorkgroupRoot $workgroupRoot -Agents Fake -StaleMinutes 1 -DryRun -Json
$result = $output | ConvertFrom-Json
if ($result.action -ne 'would_notify_orphan_lock' -or $result.message_id -ne $messageId) {
    throw "Expected orphan lock notification. Output: $output"
}

[pscustomobject]@{
    Passed = $true
    MessagePath = $messagePath
    LockPath = $lockPath
    Action = $result.action
    Status = $result.status
}
