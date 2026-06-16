param(
    [string] $ProjectRoot = (Get-Location).Path,
    [int] $TimeoutSeconds = 360
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

function Read-FrontMatterValue {
    param(
        [string[]] $Lines,
        [string] $Key
    )
    foreach ($line in $Lines) {
        if ($line -match "^$([regex]::Escape($Key)):\s*(.*)$") {
            return $Matches[1].Trim().Trim('"')
        }
    }
    return ''
}

function Find-ReportByReplyTo {
    param(
        [string] $InboxPath,
        [string] $ReplyTo,
        [datetime] $After
    )

    $reports = Get-ChildItem -LiteralPath $InboxPath -Filter '*.md' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $After } |
        Sort-Object LastWriteTime -Descending

    foreach ($report in $reports) {
        $lines = [System.IO.File]::ReadAllLines($report.FullName, [System.Text.Encoding]::UTF8)
        $reply = Read-FrontMatterValue -Lines $lines -Key 'reply_to'
        $from = Read-FrontMatterValue -Lines $lines -Key 'from'
        if ($reply -eq $ReplyTo -and $from -eq 'OpenCode') {
            return $report
        }
    }
    return $null
}

$workgroupRoot = 'docs/ai-workgroup'
$openCodeInbox = Join-Path $workgroupRoot 'inbox/OpenCode'
$codeXInbox = Join-Path $workgroupRoot 'inbox/CodeX'
$done = Join-Path $workgroupRoot 'done'
New-Item -ItemType Directory -Force -Path $openCodeInbox, $codeXInbox, $done | Out-Null

$readyMessages = New-Object System.Collections.ArrayList
foreach ($candidate in (Get-ChildItem -LiteralPath $openCodeInbox -Filter '*.md' -File -ErrorAction SilentlyContinue)) {
    $lines = [System.IO.File]::ReadAllLines($candidate.FullName, [System.Text.Encoding]::UTF8)
    $status = Read-FrontMatterValue -Lines $lines -Key 'status'
    if ($status -eq 'ready') {
        [void] $readyMessages.Add($candidate.FullName)
    }
}
if ($readyMessages.Count -gt 0) {
    throw "OpenCode inbox already contains ready messages; refusing controlled smoke test. Files: $($readyMessages -join ', ')"
}

$startedAt = Get-Date
$timestamp = New-FileTimestamp
$messageId = "OX0-msg-$timestamp"
$task = "OX0-opencode-watcher-$timestamp"
$messageName = "${timestamp}_from-CodeX_to-OpenCode_type-instruction_task-$task.md"
$messagePath = Join-Path $openCodeInbox $messageName
$createdAt = New-IsoTimestamp

$watchJob = Start-Job -ScriptBlock {
    param(
        [string] $Root,
        [int] $StopAfter
    )
    Set-Location $Root
    & 'scripts/ai-workgroup/watch-inbox.ps1' -Agents OpenCode -SkipInitialScan -AllowExternalAgents -PollSeconds 60 -StopAfterSeconds $StopAfter -Json
} -ArgumentList $resolvedRoot, $TimeoutSeconds

try {
    Start-Sleep -Seconds 2

    $message = @"
---
id: $messageId
task: $task
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
  - docs/ai-workgroup/shared/report-template.md
allowed_files: []
forbidden_files:
  - .env
  - migrations/**
  - docs/ai-workgroup/state/**
attempt: 0
max_attempts: 1
timeout_minutes: 15
review_delegate: CodeX
---

# Task

Run the OX0 OpenCode watcher smoke test.

Acceptance:

- This task is dispatched by watch-inbox.ps1, not by manually calling opencode-headless-runner.ps1.
- Runner policy allows this read-only OpenCode task.
- OpenCode writes one report to docs/ai-workgroup/inbox/CodeX/.
- The report uses reply_to: $messageId.
- No source code, tests, configuration, secrets, migrations, deployment files, or state files are modified.

Write a concise report. Mention whether the task was read through the watcher/scanner path and whether any interactive prompt blocked the run.
"@

    [System.IO.File]::WriteAllText($messagePath, $message, [System.Text.UTF8Encoding]::new($false))

    $donePath = Join-Path $done $messageName
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $report = $null

    while ((Get-Date) -lt $deadline) {
        $report = Find-ReportByReplyTo -InboxPath $codeXInbox -ReplyTo $messageId -After $startedAt
        $doneExists = Test-Path -LiteralPath $donePath -PathType Leaf
        if (($null -ne $report) -and $doneExists) {
            Stop-Job -Job $watchJob -ErrorAction SilentlyContinue
            break
        }
        if ($watchJob.State -ne 'Running') {
            break
        }
        Start-Sleep -Seconds 2
    }

    if ($watchJob.State -eq 'Running') {
        Stop-Job -Job $watchJob -ErrorAction SilentlyContinue
    }

    $watchOutput = Receive-Job -Job $watchJob -ErrorAction SilentlyContinue
    if ($null -eq $report) {
        $report = Find-ReportByReplyTo -InboxPath $codeXInbox -ReplyTo $messageId -After $startedAt
    }
    if ($null -eq $report) {
        throw "Expected OpenCode report with reply_to $messageId was not found within $TimeoutSeconds seconds. Watch output: $($watchOutput -join "`n")"
    }
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

    $usage = Get-ChildItem -LiteralPath (Join-Path $workgroupRoot 'state') -Filter 'runner-usage.*.jsonl' -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1

    [pscustomobject]@{
        Passed = $true
        MessageId = $messageId
        Report = $report.FullName
        DoneTask = (Resolve-Path -LiteralPath $donePath).ProviderPath
        UsageLog = $(if ($null -eq $usage) { '' } else { $usage.FullName })
        WatchOutput = ($watchOutput -join "`n")
    }
} finally {
    Remove-Job -Job $watchJob -Force -ErrorAction SilentlyContinue
}
