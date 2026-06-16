param(
    [string] $ProjectRoot = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
Set-Location $resolvedRoot

function New-IsoTimestamp {
    return (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
}

function New-FileTimestamp {
    return (Get-Date).ToString("yyyy-MM-ddTHHmmss")
}

$workgroupRoot = 'docs/ai-workgroup'
$fakeInbox = Join-Path $workgroupRoot 'inbox/Fake'
New-Item -ItemType Directory -Force -Path $fakeInbox | Out-Null

$timestamp = New-FileTimestamp
$messageId = "WC0-msg-$timestamp"
$task = "WC0-watch-inbox-$timestamp"
$messageName = "${timestamp}_from-CodeX_to-Fake_type-instruction_task-$task.md"
$messagePath = Join-Path $fakeInbox $messageName
$createdAt = New-IsoTimestamp

$watchJob = Start-Job -ScriptBlock {
    param([string] $Root)
    Set-Location $Root
    & 'scripts/ai-workgroup/watch-inbox.ps1' -Agents Fake -SkipInitialScan -PollSeconds 60 -StopAfterSeconds 12 -Json
} -ArgumentList $resolvedRoot

try {
    Start-Sleep -Seconds 2

    $message = @"
---
id: $messageId
task: $task
from: CodeX
to: Fake
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
  - migrations/**
  - docs/ai-workgroup/state/**
attempt: 0
max_attempts: 1
timeout_minutes: 5
review_delegate: CodeX
---

# Task

Verify that `watch-inbox.ps1` receives a FileSystemWatcher event and dispatches the Fake runner.
"@

    [System.IO.File]::WriteAllText($messagePath, $message, [System.Text.UTF8Encoding]::new($false))

    Wait-Job -Job $watchJob -Timeout 20 | Out-Null
    if ($watchJob.State -eq 'Running') {
        Stop-Job -Job $watchJob
        throw 'watch-inbox smoke test timed out.'
    }

    $watchOutput = Receive-Job -Job $watchJob

    $report = Get-ChildItem -LiteralPath (Join-Path $workgroupRoot 'inbox/CodeX') -Filter "*task-$task`_fake-runner-report.md" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $report) {
        throw "Expected Fake report was not written for $task. Watch output: $($watchOutput -join "`n")"
    }

    $donePath = Join-Path (Join-Path $workgroupRoot 'done') $messageName
    if (-not (Test-Path -LiteralPath $donePath -PathType Leaf)) {
        throw "Expected done task was not found: $donePath"
    }

    $reportValidation = & 'scripts/ai-workgroup/validate-message.ps1' -Path $report.FullName 2>&1
    if (-not $?) {
        throw "Report validation failed: $($reportValidation -join "`n")"
    }

    $doneValidation = & 'scripts/ai-workgroup/validate-message.ps1' -Path $donePath 2>&1
    if (-not $?) {
        throw "Done task validation failed: $($doneValidation -join "`n")"
    }

    [pscustomobject]@{
        Passed = $true
        MessageId = $messageId
        Report = $report.FullName
        DoneTask = (Resolve-Path -LiteralPath $donePath).ProviderPath
        WatchOutput = ($watchOutput -join "`n")
    }
} finally {
    Remove-Job -Job $watchJob -Force -ErrorAction SilentlyContinue
}
