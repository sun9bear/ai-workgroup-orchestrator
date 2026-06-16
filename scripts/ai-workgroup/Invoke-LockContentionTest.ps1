param(
    [string] $ProjectRoot = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'

$resolvedProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$tmpRoot = [System.IO.Path]::GetFullPath((Join-Path $resolvedProjectRoot 'tests/tmp/lock-contention'))
if (-not $tmpRoot.StartsWith($resolvedProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use temp path outside project root: $tmpRoot"
}

if (Test-Path -LiteralPath $tmpRoot) {
    Remove-Item -LiteralPath $tmpRoot -Recurse -Force
}

$workgroupRoot = Join-Path $tmpRoot 'docs/ai-workgroup'
$inbox = Join-Path $workgroupRoot 'inbox/Fake'
$working = Join-Path $workgroupRoot 'working/Fake'
$locks = Join-Path $workgroupRoot 'state/locks'
New-Item -ItemType Directory -Force -Path $inbox, $working, $locks | Out-Null

$messagePath = Join-Path $inbox '2026-05-27T130000_from-CodeX_to-Fake_type-review_task-LC0_lock-contention.md'
$message = @'
---
id: LC0-msg-001
task: LC0
from: CodeX
to: Fake
type: review
status: ready
priority: medium
reply_to: ""
requires_human: false
created_at: 2026-05-27T13:00:00+08:00
can_write: false
context_files: []
allowed_files: []
forbidden_files:
  - .env
attempt: 0
max_attempts: 1
timeout_minutes: 5
---

# Lock Contention Fixture

Two runners will attempt to claim this same message.
'@
[System.IO.File]::WriteAllText($messagePath, $message, [System.Text.UTF8Encoding]::new($false))

$claimScript = Join-Path $resolvedProjectRoot 'scripts/ai-workgroup/claim-task.ps1'

$jobA = Start-Job -ScriptBlock {
    param($script, $root)
    $output = & powershell -NoProfile -ExecutionPolicy Bypass -File $script -Agent Fake -WorkgroupRoot $root -MessageId LC0-msg-001 -RunnerId runner-a -HoldMilliseconds 500 -Json 2>&1
    [pscustomobject]@{ Runner = 'runner-a'; ExitCode = $LASTEXITCODE; Output = ($output -join "`n") }
} -ArgumentList $claimScript, $workgroupRoot

$jobB = Start-Job -ScriptBlock {
    param($script, $root)
    $output = & powershell -NoProfile -ExecutionPolicy Bypass -File $script -Agent Fake -WorkgroupRoot $root -MessageId LC0-msg-001 -RunnerId runner-b -HoldMilliseconds 500 -Json 2>&1
    [pscustomobject]@{ Runner = 'runner-b'; ExitCode = $LASTEXITCODE; Output = ($output -join "`n") }
} -ArgumentList $claimScript, $workgroupRoot

Wait-Job -Job $jobA, $jobB | Out-Null
$results = @(Receive-Job -Job $jobA, $jobB)
Remove-Job -Job $jobA, $jobB

$successes = @($results | Where-Object { $_.ExitCode -eq 0 -and $_.Output -match '"claimed":true' })
$locked = @($results | Where-Object { $_.ExitCode -eq 2 -and $_.Output -match '"reason":"locked"' })

if ($successes.Count -ne 1) {
    $results | Format-List | Out-String | Write-Output
    throw "Expected exactly one successful claim, got $($successes.Count)."
}
if ($locked.Count -ne 1) {
    $results | Format-List | Out-String | Write-Output
    throw "Expected exactly one locked loser, got $($locked.Count)."
}

$inboxCount = @(Get-ChildItem -LiteralPath $inbox -Filter '*.md' -File -ErrorAction SilentlyContinue).Count
$workingCount = @(Get-ChildItem -LiteralPath $working -Filter '*.md' -File -ErrorAction SilentlyContinue).Count
$lockCount = @(Get-ChildItem -LiteralPath $locks -Filter '*.lock' -File -ErrorAction SilentlyContinue).Count

if ($inboxCount -ne 0) {
    throw "Expected empty inbox after successful claim, found $inboxCount message(s)."
}
if ($workingCount -ne 1) {
    throw "Expected exactly one working message, found $workingCount."
}
if ($lockCount -ne 1) {
    throw "Expected exactly one lock file, found $lockCount."
}

[pscustomobject]@{
    Passed = $true
    SuccessfulRunner = $successes[0].Runner
    LockedRunner = $locked[0].Runner
    WorkgroupRoot = $workgroupRoot
}
