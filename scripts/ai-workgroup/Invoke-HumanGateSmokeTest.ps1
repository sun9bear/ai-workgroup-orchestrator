param(
    [string] $ProjectRoot = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
Set-Location $resolvedRoot

$tmpRoot = Join-Path $resolvedRoot 'tests/tmp/human-gate'
if ((Test-Path -LiteralPath $tmpRoot) -and -not ((Resolve-Path -LiteralPath $tmpRoot).ProviderPath.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase))) {
    throw "Refusing to clean temp path outside project: $tmpRoot"
}
Remove-Item -LiteralPath $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue

$workgroupRoot = Join-Path $tmpRoot 'docs/ai-workgroup'
$fakeInbox = Join-Path $workgroupRoot 'inbox/Fake'
$shared = Join-Path $workgroupRoot 'shared'
New-Item -ItemType Directory -Force -Path $fakeInbox, $shared | Out-Null

$messagePath = Join-Path $fakeInbox '2026-05-27T210000_from-CodeX_to-Fake_type-instruction_task-HG0_human-gate.md'
$message = @'
---
id: HG0-msg-001
task: HG0-human-gate
from: CodeX
to: Fake
type: instruction
status: ready
priority: high
reply_to: ""
requires_human: true
created_at: 2026-05-27T21:00:00+08:00
can_write: false
context_files: []
allowed_files: []
forbidden_files:
  - .env
attempt: 0
max_attempts: 1
timeout_minutes: 5
---

# Human Gate Smoke

This task must not be dispatched automatically.
'@
[System.IO.File]::WriteAllText($messagePath, $message, [System.Text.UTF8Encoding]::new($false))

$policyPath = Join-Path $shared 'runner-policy.json'
$policy = @'
{
  "kill_switch": false,
  "agents": {
    "Fake": {
      "enabled": true,
      "external": false,
      "daily_limit": 10,
      "timeout_seconds": 60,
      "allow_write": true,
      "max_budget_usd": 0
    }
  }
}
'@
[System.IO.File]::WriteAllText($policyPath, $policy, [System.Text.UTF8Encoding]::new($false))

$scanOutput = & 'scripts/ai-workgroup/scan-inbox.ps1' -WorkgroupRoot $workgroupRoot -Agents Fake -DryRun -Json
$scan = $scanOutput | ConvertFrom-Json
if ($scan.action -ne 'skip' -or $scan.status -ne 'requires_human') {
    throw "Expected scan to skip requires_human task. Output: $scanOutput"
}

$policyOutput = & 'scripts/ai-workgroup/check-runner-policy.ps1' -Agent Fake -MessagePath $messagePath -WorkgroupRoot $workgroupRoot -PolicyPath $policyPath -Json
$policyResult = $policyOutput | ConvertFrom-Json
if ($policyResult.allowed -ne $false -or 'requires_human' -notin @($policyResult.reasons)) {
    throw "Expected runner policy to deny requires_human task. Output: $policyOutput"
}

[pscustomobject]@{
    Passed = $true
    MessagePath = $messagePath
    ScanAction = $scan.action
    ScanStatus = $scan.status
    PolicyAllowed = $policyResult.allowed
    PolicyReasons = @($policyResult.reasons)
}
