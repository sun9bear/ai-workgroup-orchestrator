param(
    [string] $Agent = 'OpenCode',
    [string] $WorkgroupRoot = 'docs/ai-workgroup',
    [string] $PromptTemplate = 'scripts/ai-workgroup/prompts/opencode-wrapper-runner.md',
    [string] $OpenCodePath = "$env:LOCALAPPDATA\OpenCode\opencode-cli.exe",
    [string] $OpenCodeAgent = '',
    [string] $Model = '',
    [string] $MessageId = '',
    [int] $TimeoutSeconds = 900,
    [switch] $DangerouslySkipPermissions,
    [switch] $DryRun
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

if (-not (Test-Path -LiteralPath $OpenCodePath -PathType Leaf)) {
    throw "OpenCode CLI was not found at $OpenCodePath"
}

if (-not (Test-Path -LiteralPath $PromptTemplate -PathType Leaf)) {
    throw "Prompt template was not found at $PromptTemplate"
}

$inbox = Join-Path $WorkgroupRoot "inbox/$Agent"
$done = Join-Path $WorkgroupRoot 'done'
$reportDir = Join-Path $WorkgroupRoot 'inbox/CodeX'
$eventsPath = Join-Path $WorkgroupRoot "state/events.$Agent.jsonl"
$claimScript = 'scripts/ai-workgroup/claim-task.ps1'

New-Item -ItemType Directory -Force -Path $done, $reportDir, (Split-Path $eventsPath -Parent) | Out-Null

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

$prompt = [System.IO.File]::ReadAllText($PromptTemplate, [System.Text.Encoding]::UTF8)
$taskForPrompt = $candidate.FullName -replace '\\', '/'
$prompt = $prompt.Replace('{{TASK_FILE}}', $taskForPrompt)
$prompt = $prompt.Replace('{{WORKGROUP_ROOT}}', ($WorkgroupRoot -replace '\\', '/'))
$prompt = $prompt.Replace('{{REPORT_DIR}}', ($reportDir -replace '\\', '/'))

$opencodeArgs = New-Object System.Collections.ArrayList
[void] $opencodeArgs.Add('run')
[void] $opencodeArgs.Add($prompt)
[void] $opencodeArgs.Add('--file')
[void] $opencodeArgs.Add($candidate.FullName)
[void] $opencodeArgs.Add('--dir')
[void] $opencodeArgs.Add((Get-Location).Path)
[void] $opencodeArgs.Add('--format')
[void] $opencodeArgs.Add('json')
if (-not [string]::IsNullOrWhiteSpace($OpenCodeAgent)) {
    [void] $opencodeArgs.Add('--agent')
    [void] $opencodeArgs.Add($OpenCodeAgent)
}
if (-not [string]::IsNullOrWhiteSpace($Model)) {
    [void] $opencodeArgs.Add('--model')
    [void] $opencodeArgs.Add($Model)
}
if ($DangerouslySkipPermissions) {
    [void] $opencodeArgs.Add('--dangerously-skip-permissions')
}

if ($DryRun) {
    [pscustomobject]@{
        dry_run = $true
        message_id = $candidateId
        opencode_path = $OpenCodePath
        argv = @($opencodeArgs)
    } | ConvertTo-Json -Depth 8
    exit 0
}

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

$workingPath = $claim.working_path
$lockPath = $claim.lock_path
$prompt = $prompt.Replace($taskForPrompt, ($workingPath -replace '\\', '/'))
$opencodeArgs[1] = $prompt
$fileArgIndex = [array]::IndexOf(@($opencodeArgs), '--file')
$opencodeArgs[$fileArgIndex + 1] = $workingPath

Write-Event -Type 'command_started' -MessageId $candidateId -Path $workingPath

$job = Start-Job -ScriptBlock {
    param(
        [string] $Executable,
        [object[]] $Arguments
    )
    $commandOutput = & $Executable @Arguments 2>&1
    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output = ($commandOutput -join "`n")
    }
} -ArgumentList $OpenCodePath, @($opencodeArgs)

$finished = Wait-Job -Job $job -Timeout $TimeoutSeconds
if ($null -eq $finished) {
    Stop-Job -Job $job
    Remove-Job -Job $job
    Write-Event -Type 'session_failed' -MessageId $candidateId -Path $workingPath -Status "timeout_${TimeoutSeconds}s"
    throw "OpenCode headless timed out after $TimeoutSeconds seconds."
}

$result = Receive-Job -Job $job
Remove-Job -Job $job

if ($result.ExitCode -ne 0) {
    Write-Event -Type 'session_failed' -MessageId $candidateId -Path $workingPath -Status "opencode_exit_$($result.ExitCode)"
    throw "OpenCode headless failed with exit code $($result.ExitCode). Output: $($result.Output)"
}

Write-Event -Type 'command_finished' -MessageId $candidateId -Path $workingPath -Status 'ok'

$reportPath = ''
foreach ($line in @($result.Output -split "`r?`n")) {
    if ($line -match 'docs[/\\]ai-workgroup[/\\]inbox[/\\]CodeX[/\\][^`\s]+\.md') {
        $reportPath = $Matches[0] -replace '/', '\'
        break
    }
}

if ([string]::IsNullOrWhiteSpace($reportPath)) {
    $candidateReport = Get-ChildItem -LiteralPath $reportDir -Filter "*from-OpenCode*task-$candidateId*.md" -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -ne $candidateReport) {
        $reportPath = $candidateReport.FullName
    }
}

if (-not [string]::IsNullOrWhiteSpace($reportPath)) {
    Write-Event -Type 'report_written' -MessageId $candidateId -Path $reportPath
    & 'scripts/ai-workgroup/validate-message.ps1' -Path $reportPath | Out-Host
}

$donePath = Join-Path $done (Split-Path $workingPath -Leaf)
Set-FrontMatterValue -FilePath $workingPath -Key 'status' -Value 'done'
Set-FrontMatterValue -FilePath $workingPath -Key 'completed_at' -Value (New-IsoTimestamp)
Move-Item -LiteralPath $workingPath -Destination $donePath
if (-not [string]::IsNullOrWhiteSpace($lockPath) -and (Test-Path -LiteralPath $lockPath)) {
    Remove-Item -LiteralPath $lockPath -Force
    Write-Event -Type 'lock_released' -MessageId $candidateId -Path $lockPath
}
Write-Event -Type 'session_finished' -MessageId $candidateId -Path $donePath -Status 'ok'

Write-Output $result.Output
Write-Output "OpenCode wrapper completed for $candidateId"
