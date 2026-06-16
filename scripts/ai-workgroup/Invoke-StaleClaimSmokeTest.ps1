param(
    [string] $ProjectRoot = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
Set-Location $resolvedRoot

function New-IsoTimestamp {
    param([datetime] $Date = (Get-Date))
    return $Date.ToString("yyyy-MM-ddTHH:mm:ssK")
}

$tmpRoot = Join-Path $resolvedRoot 'tests/tmp/stale-claim'
if (Test-Path -LiteralPath $tmpRoot) {
    $resolvedTmp = (Resolve-Path -LiteralPath $tmpRoot).ProviderPath
    if (-not $resolvedTmp.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside project root: $resolvedTmp"
    }
    Remove-Item -LiteralPath $resolvedTmp -Recurse -Force
}

$workgroupRoot = Join-Path $tmpRoot 'docs/ai-workgroup'
$workingFake = Join-Path $workgroupRoot 'working/Fake'
$codeXInbox = Join-Path $workgroupRoot 'inbox/CodeX'
$locks = Join-Path $workgroupRoot 'state/locks'
New-Item -ItemType Directory -Force -Path $workingFake, $codeXInbox, $locks | Out-Null

$messageId = 'ST0-msg-001'
$task = 'ST0-stale-claim'
$lockId = $messageId
$claimedAt = New-IsoTimestamp -Date ((Get-Date).AddHours(-3))
$createdAt = New-IsoTimestamp -Date ((Get-Date).AddHours(-4))
$workingPath = Join-Path $workingFake '2026-05-27T170000_from-CodeX_to-Fake_type-instruction_task-ST0_stale-claim.md'
$lockPath = Join-Path $locks "$lockId.lock"

$message = @"
---
id: $messageId
task: $task
from: CodeX
to: Fake
type: instruction
status: claimed
priority: high
reply_to: ""
requires_human: false
created_at: $createdAt
can_write: true
context_files:
  - docs/ai-workgroup/00-protocol.md
allowed_files:
  - tests/**
forbidden_files:
  - .env
  - migrations/**
attempt: 0
max_attempts: 1
timeout_minutes: 5
review_delegate: CodeX
claimed_by: stale-smoke-runner
claimed_at: $claimedAt
lock_id: $lockId
---

# Task

This is a synthetic stale claimed task for `check-stale-claims.ps1`.
"@

[System.IO.File]::WriteAllText($workingPath, $message, [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText($lockPath, '{"message_id":"ST0-msg-001","agent":"Fake"}', [System.Text.UTF8Encoding]::new($false))

$firstOutput = & 'scripts/ai-workgroup/check-stale-claims.ps1' -WorkgroupRoot $workgroupRoot -Agents Fake -StaleMinutes 60 -Json
if (-not $?) {
    throw "First stale check failed: $($firstOutput -join "`n")"
}

$first = $firstOutput | ConvertFrom-Json
if ($null -eq $first -or $first.action -ne 'notified') {
    throw "Expected first stale check to notify. Output: $($firstOutput -join "`n")"
}

$notificationPath = $first.notification_path
if (-not (Test-Path -LiteralPath $notificationPath -PathType Leaf)) {
    throw "Expected notification was not written: $notificationPath"
}

$validation = & 'scripts/ai-workgroup/validate-message.ps1' -Path $notificationPath 2>&1
if (-not $?) {
    throw "Notification validation failed: $($validation -join "`n")"
}

$secondOutput = & 'scripts/ai-workgroup/check-stale-claims.ps1' -WorkgroupRoot $workgroupRoot -Agents Fake -StaleMinutes 60 -Json
if (-not $?) {
    throw "Second stale check failed: $($secondOutput -join "`n")"
}
$second = $secondOutput | ConvertFrom-Json
if ($null -eq $second -or $second.action -ne 'skip_duplicate') {
    throw "Expected second stale check to skip duplicate. Output: $($secondOutput -join "`n")"
}

if (-not (Test-Path -LiteralPath $workingPath -PathType Leaf)) {
    throw "Working file was unexpectedly moved or removed."
}
if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
    throw "Lock file was unexpectedly removed."
}

$notificationLines = [System.IO.File]::ReadAllLines($notificationPath, [System.Text.Encoding]::UTF8)
$requiresHumanLine = @($notificationLines | Where-Object { $_ -match '^requires_human:' }) | Select-Object -First 1
if ($requiresHumanLine -ne 'requires_human: true') {
    throw "Expected notification requires_human: true for can_write task, got: $requiresHumanLine"
}

[pscustomobject]@{
    Passed = $true
    MessageId = $messageId
    Notification = (Resolve-Path -LiteralPath $notificationPath).ProviderPath
    WorkingFileStillExists = (Test-Path -LiteralPath $workingPath -PathType Leaf)
    LockFileStillExists = (Test-Path -LiteralPath $lockPath -PathType Leaf)
    DuplicateAction = $second.action
    WorkgroupRoot = $workgroupRoot
}
