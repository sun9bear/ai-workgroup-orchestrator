param(
    [string] $Agent = 'Claude-Code',
    [string] $WorkgroupRoot = 'docs/ai-workgroup',
    [string] $PromptTemplate = 'scripts/ai-workgroup/prompts/claude-wrapper-runner.md',
    [decimal] $MaxBudgetUsd = 1.00,
    [int] $TimeoutSeconds = 900,
    [string] $MessageId = ''
)

$ErrorActionPreference = 'Stop'

function New-IsoTimestamp {
    return (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
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

function Set-FrontMatterValue {
    param(
        [string] $FilePath,
        [string] $Key,
        [string] $Value
    )

    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($line in [System.IO.File]::ReadAllLines($FilePath, [System.Text.Encoding]::UTF8)) {
        $lines.Add($line)
    }

    if ($lines.Count -lt 2 -or $lines[0] -ne '---') {
        throw "File does not have YAML front matter: $FilePath"
    }

    $endIndex = -1
    for ($i = 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -eq '---') {
            $endIndex = $i
            break
        }
    }
    if ($endIndex -lt 0) {
        throw "File front matter is not closed: $FilePath"
    }

    $updated = $false
    for ($i = 1; $i -lt $endIndex; $i++) {
        if ($lines[$i] -match "^$([regex]::Escape($Key)):\s*") {
            $lines[$i] = "${Key}: $Value"
            $updated = $true
            break
        }
    }

    if (-not $updated) {
        $lines.Insert($endIndex, "${Key}: $Value")
    }

    [System.IO.File]::WriteAllLines($FilePath, $lines, [System.Text.UTF8Encoding]::new($false))
}

function Write-Event {
    param(
        [string] $Type,
        [string] $MessageId,
        [string] $Path = '',
        [string] $Status = ''
    )
    $event = [ordered]@{
        type = $Type
        agent = $Agent
        message_id = $MessageId
        at = (New-IsoTimestamp)
    }
    if (-not [string]::IsNullOrWhiteSpace($Path)) {
        $event.path = $Path
    }
    if (-not [string]::IsNullOrWhiteSpace($Status)) {
        $event.status = $Status
    }
    $event | ConvertTo-Json -Compress | Add-Content -LiteralPath $eventsPath -Encoding UTF8
}

function Get-CandidateMessage {
    param(
        [string] $InboxPath,
        [string] $WantedMessageId
    )

    $candidateMessages = Get-ChildItem -LiteralPath $InboxPath -Filter '*.md' -File -ErrorAction SilentlyContinue |
        Sort-Object Name
    if (-not [string]::IsNullOrWhiteSpace($WantedMessageId)) {
        $matchedMessages = New-Object System.Collections.ArrayList
        foreach ($candidate in $candidateMessages) {
            $candidateLines = [System.IO.File]::ReadAllLines($candidate.FullName, [System.Text.Encoding]::UTF8)
            $candidateId = Read-FrontMatterValue -Lines $candidateLines -Key 'id'
            if ($candidateId -eq $WantedMessageId) {
                [void] $matchedMessages.Add($candidate)
            }
        }
        $candidateMessages = @($matchedMessages)
    }
    return @($candidateMessages) | Select-Object -First 1
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
        if ($reply -eq $ReplyTo -and $from -eq 'Claude-Code') {
            return $report
        }
    }
    return $null
}

if (-not (Test-Path -LiteralPath $PromptTemplate -PathType Leaf)) {
    throw "Prompt template was not found at $PromptTemplate"
}

$inbox = Join-Path $WorkgroupRoot "inbox/$Agent"
$done = Join-Path $WorkgroupRoot 'done'
$reportDir = Join-Path $WorkgroupRoot 'inbox/CodeX'
$diagnosticsDir = Join-Path $WorkgroupRoot 'state/diagnostics'
$eventsPath = Join-Path $WorkgroupRoot "state/events.$Agent.jsonl"
$claimScript = 'scripts/ai-workgroup/claim-task.ps1'

New-Item -ItemType Directory -Force -Path $done, $reportDir, $diagnosticsDir, (Split-Path $eventsPath -Parent) | Out-Null

$candidate = Get-CandidateMessage -InboxPath $inbox -WantedMessageId $MessageId
if ($null -eq $candidate) {
    if ([string]::IsNullOrWhiteSpace($MessageId)) {
        Write-Output "No ready messages for $Agent."
    } else {
        Write-Output "No ready message for $Agent with id $MessageId."
    }
    exit 0
}

$candidateLines = [System.IO.File]::ReadAllLines($candidate.FullName, [System.Text.Encoding]::UTF8)
$candidateId = Read-FrontMatterValue -Lines $candidateLines -Key 'id'
if ([string]::IsNullOrWhiteSpace($candidateId)) {
    throw "Message id is missing in $($candidate.FullName)"
}

$promptTemplateText = [System.IO.File]::ReadAllText($PromptTemplate, [System.Text.Encoding]::UTF8)

$runnerId = "$env:COMPUTERNAME-$PID"
$claimOutput = & $claimScript -Agent $Agent -WorkgroupRoot $WorkgroupRoot -MessageId $candidateId -RunnerId $runnerId -Json
$claimExitCode = $LASTEXITCODE
if ($claimExitCode -ne 0) {
    Write-Output $claimOutput
    exit $claimExitCode
}

$claim = $claimOutput | ConvertFrom-Json
if (-not $claim.claimed) {
    Write-Output $claimOutput
    exit 0
}

$workingPath = (Resolve-Path -LiteralPath $claim.working_path).ProviderPath
$lockPath = (Resolve-Path -LiteralPath $claim.lock_path).ProviderPath
$workgroupRootForPrompt = (Resolve-Path -LiteralPath $WorkgroupRoot).ProviderPath -replace '\\', '/'
$reportDirForPrompt = (Resolve-Path -LiteralPath $reportDir).ProviderPath -replace '\\', '/'
$taskForPrompt = $workingPath -replace '\\', '/'
$taskBody = [System.IO.File]::ReadAllText($workingPath, [System.Text.Encoding]::UTF8)
$prompt = $promptTemplateText.Replace('{{TASK_FILE}}', $taskForPrompt)
$prompt = $prompt.Replace('{{TASK_BODY}}', $taskBody)
$prompt = $prompt.Replace('{{WORKGROUP_ROOT}}', $workgroupRootForPrompt)
$prompt = $prompt.Replace('{{REPORT_DIR}}', $reportDirForPrompt)

$commandStartedAt = Get-Date
Write-Event -Type 'command_started' -MessageId $candidateId -Path $workingPath

$job = Start-Job -ScriptBlock {
    param(
        [string] $Root,
        [string] $Prompt,
        [decimal] $Budget
    )
    Set-Location $Root
    $commandOutput = $Prompt | claude -p --max-budget-usd $Budget --permission-mode acceptEdits --tools "Read,Write" 2>&1
    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output = ($commandOutput -join "`n")
    }
} -ArgumentList (Get-Location).Path, $prompt, $MaxBudgetUsd

$finished = Wait-Job -Job $job -Timeout $TimeoutSeconds
if ($null -eq $finished) {
    Stop-Job -Job $job
    Remove-Job -Job $job
    Write-Event -Type 'session_failed' -MessageId $candidateId -Path $workingPath -Status "timeout_${TimeoutSeconds}s"
    throw "Claude headless timed out after $TimeoutSeconds seconds."
}

$result = Receive-Job -Job $job
Remove-Job -Job $job

$claudeOutput = $result.Output
$exitCode = $result.ExitCode

if ($exitCode -ne 0) {
    Write-Event -Type 'session_failed' -MessageId $candidateId -Path $workingPath -Status "claude_exit_$exitCode"
    throw "Claude headless failed with exit code $exitCode. Output: $claudeOutput"
}

Write-Event -Type 'command_finished' -MessageId $candidateId -Path $workingPath -Status 'ok'

$reportPath = ''
foreach ($line in @($claudeOutput -split "`r?`n")) {
    if ($line -match 'docs[/\\]ai-workgroup[/\\]inbox[/\\]CodeX[/\\][^`\s]+\.md') {
        $reportPath = $Matches[0] -replace '/', '\'
        break
    }
}

if ([string]::IsNullOrWhiteSpace($reportPath)) {
    $candidateReport = Find-ReportByReplyTo -InboxPath $reportDir -ReplyTo $candidateId -After $commandStartedAt
    if ($null -ne $candidateReport) {
        $reportPath = $candidateReport.FullName
    }
}

if ([string]::IsNullOrWhiteSpace($reportPath) -or -not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
    $diagnosticPath = Join-Path $diagnosticsDir "$candidateId.claude-output.txt"
    [System.IO.File]::WriteAllText($diagnosticPath, $claudeOutput, [System.Text.UTF8Encoding]::new($false))
    Write-Event -Type 'session_failed' -MessageId $candidateId -Path $workingPath -Status "no_report diagnostic=$diagnosticPath"
    throw "Claude headless completed but no report with reply_to $candidateId was found. Diagnostic: $diagnosticPath"
}

Write-Event -Type 'report_written' -MessageId $candidateId -Path $reportPath
& 'scripts/ai-workgroup/validate-message.ps1' -Path $reportPath | Out-Host

$donePath = Join-Path $done (Split-Path $workingPath -Leaf)
Set-FrontMatterValue -FilePath $workingPath -Key 'status' -Value 'done'
Set-FrontMatterValue -FilePath $workingPath -Key 'completed_at' -Value (New-IsoTimestamp)
Move-Item -LiteralPath $workingPath -Destination $donePath
if (-not [string]::IsNullOrWhiteSpace($lockPath) -and (Test-Path -LiteralPath $lockPath)) {
    Remove-Item -LiteralPath $lockPath -Force
    Write-Event -Type 'lock_released' -MessageId $candidateId -Path $lockPath
}
Write-Event -Type 'session_finished' -MessageId $candidateId -Path $donePath -Status 'ok'

Write-Output $claudeOutput
Write-Output "Claude wrapper completed for $candidateId"
