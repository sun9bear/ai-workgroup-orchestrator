param(
    [string] $ProjectRoot = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
Set-Location $resolvedRoot

function New-IsoTimestamp {
    return (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
}

$tmpRoot = Join-Path $resolvedRoot 'tests/tmp/runner-policy'
if (Test-Path -LiteralPath $tmpRoot) {
    $resolvedTmp = (Resolve-Path -LiteralPath $tmpRoot).ProviderPath
    if (-not $resolvedTmp.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside project root: $resolvedTmp"
    }
    Remove-Item -LiteralPath $resolvedTmp -Recurse -Force
}

$workgroupRoot = Join-Path $tmpRoot 'docs/ai-workgroup'
$shared = Join-Path $workgroupRoot 'shared'
$openCodeInbox = Join-Path $workgroupRoot 'inbox/OpenCode'
New-Item -ItemType Directory -Force -Path $shared, $openCodeInbox | Out-Null

$policyPath = Join-Path $shared 'runner-policy.json'
$policy = @"
{
  "kill_switch": false,
  "agents": {
    "OpenCode": {
      "enabled": true,
      "external": true,
      "daily_limit": 1,
      "timeout_seconds": 123,
      "allow_write": false,
      "max_budget_usd": 0.25
    }
  }
}
"@
[System.IO.File]::WriteAllText($policyPath, $policy, [System.Text.UTF8Encoding]::new($false))

$createdAt = New-IsoTimestamp
$readOnlyPath = Join-Path $openCodeInbox '2026-05-27T171000_from-CodeX_to-OpenCode_type-instruction_task-PG0_readonly.md'
$readOnly = @"
---
id: PG0-msg-001
task: PG0-policy-readonly
from: CodeX
to: OpenCode
type: instruction
status: ready
priority: medium
reply_to: ""
requires_human: false
created_at: $createdAt
can_write: false
context_files:
  - docs/ai-workgroup/00-protocol.md
allowed_files: []
forbidden_files:
  - .env
attempt: 0
max_attempts: 1
timeout_minutes: 5
review_delegate: CodeX
---

# Task

Read-only policy fixture.
"@
[System.IO.File]::WriteAllText($readOnlyPath, $readOnly, [System.Text.UTF8Encoding]::new($false))

$writePath = Join-Path $openCodeInbox '2026-05-27T171100_from-CodeX_to-OpenCode_type-instruction_task-PG0_write-deny.md'
$writeTask = @"
---
id: PG0-msg-002
task: PG0-policy-write-deny
from: CodeX
to: OpenCode
type: instruction
status: ready
priority: medium
reply_to: ""
requires_human: false
created_at: $createdAt
can_write: true
context_files:
  - docs/ai-workgroup/00-protocol.md
allowed_files:
  - docs/**
forbidden_files:
  - .env
attempt: 0
max_attempts: 1
timeout_minutes: 5
review_delegate: CodeX
---

# Task

Write policy denial fixture.
"@
[System.IO.File]::WriteAllText($writePath, $writeTask, [System.Text.UTF8Encoding]::new($false))

$writeDenied = & 'scripts/ai-workgroup/check-runner-policy.ps1' -Agent OpenCode -MessagePath $writePath -WorkgroupRoot $workgroupRoot -PolicyPath $policyPath -Json | ConvertFrom-Json
if ($writeDenied.allowed -ne $false -or $writeDenied.reasons -notcontains 'write_tasks_disabled_for_agent') {
    throw "Expected write task to be denied by policy. Result: $($writeDenied | ConvertTo-Json -Compress)"
}

$firstAllowed = & 'scripts/ai-workgroup/check-runner-policy.ps1' -Agent OpenCode -MessagePath $readOnlyPath -WorkgroupRoot $workgroupRoot -PolicyPath $policyPath -Record -Json | ConvertFrom-Json
if ($firstAllowed.allowed -ne $true -or $firstAllowed.used_today -ne 1 -or $firstAllowed.timeout_seconds -ne 123) {
    throw "Expected first read-only record to be allowed. Result: $($firstAllowed | ConvertTo-Json -Compress)"
}

$limitDenied = & 'scripts/ai-workgroup/check-runner-policy.ps1' -Agent OpenCode -MessagePath $readOnlyPath -WorkgroupRoot $workgroupRoot -PolicyPath $policyPath -Record -Json | ConvertFrom-Json
if ($limitDenied.allowed -ne $false -or -not (($limitDenied.reasons -join ',') -match 'daily_limit_reached')) {
    throw "Expected second read-only record to hit daily limit. Result: $($limitDenied | ConvertTo-Json -Compress)"
}

Remove-Item -LiteralPath $readOnlyPath -Force
Remove-Item -LiteralPath (Join-Path $workgroupRoot 'state') -Recurse -Force
$scanResult = & 'scripts/ai-workgroup/scan-inbox.ps1' -WorkgroupRoot $workgroupRoot -Agents OpenCode -AllowExternalAgents -PolicyPath $policyPath -Json | ConvertFrom-Json
if ($scanResult.action -ne 'dispatch_failed' -or $scanResult.status -notmatch 'write_tasks_disabled_for_agent') {
    throw "Expected scanner to block OpenCode before external dispatch. Result: $($scanResult | ConvertTo-Json -Compress)"
}

[pscustomobject]@{
    Passed = $true
    WorkgroupRoot = $workgroupRoot
    PolicyPath = $policyPath
    WriteDeniedReason = ($writeDenied.reasons -join ',')
    FirstAllowedUsedToday = $firstAllowed.used_today
    LimitDeniedReason = ($limitDenied.reasons -join ',')
    ScannerAction = $scanResult.action
}
