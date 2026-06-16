param(
    [Parameter(Mandatory = $true)]
    [string] $ProjectRoot,
    [string] $WorkgroupRelativePath = 'docs/ai-workgroup',
    [string] $MessageId = '',
    [decimal] $MaxBudgetUsd = 1.50,
    [int] $TimeoutSeconds = 900,
    [switch] $DryRun,
    [switch] $Json
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

function Get-CandidateMessage {
    param(
        [Parameter(Mandatory = $true)][string] $InboxPath,
        [string] $WantedMessageId = ''
    )

    $candidateMessages = Get-ChildItem -LiteralPath $InboxPath -Filter '*.md' -File -ErrorAction SilentlyContinue |
        Sort-Object Name

    foreach ($candidate in @($candidateMessages)) {
        $frontMatter = Read-AiwgFrontMatter -FilePath $candidate.FullName
        $candidateId = [string](Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'id')
        $status = [string](Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'status')
        $type = [string](Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'type')
        if (-not [string]::IsNullOrWhiteSpace($WantedMessageId) -and $candidateId -ne $WantedMessageId) {
            continue
        }
        if ($status -ne 'ready' -or $type -ne 'advisory') {
            continue
        }
        return [pscustomobject]@{
            File = $candidate
            FrontMatter = $frontMatter
            Id = $candidateId
        }
    }
    return $null
}

function Write-Result {
    param($Result)
    if ($Json) {
        $Result | ConvertTo-Json -Depth 8
    } else {
        if ($Result.ok) {
            Write-Output "OK $($Result.action) $($Result.message_id)"
        } else {
            Write-Output "ERR $($Result.action) $($Result.message_id): $($Result.reason)"
        }
        if ($Result.path) {
            Write-Output $Result.path
        }
    }
}

function Format-ListForPrompt {
    param([string[]] $Values)
    if ($null -eq $Values -or $Values.Count -eq 0) {
        return '- none'
    }
    return (($Values | ForEach-Object { "- $_" }) -join "`n")
}

function ConvertTo-SafeFileName {
    param([string] $Value)
    return (($Value -replace '[^A-Za-z0-9_.-]', '_').Trim('_'))
}

function Invoke-GitLines {
    param(
        [Parameter(Mandatory = $true)][string] $Root,
        [Parameter(Mandatory = $true)][string[]] $Arguments
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & git -C $Root @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code ${exitCode}: $($output -join "`n")"
    }
    return @($output | Where-Object {
        $line = [string]$_
        -not [string]::IsNullOrWhiteSpace($line) -and -not $line.StartsWith('warning:')
    })
}

function Get-CurrentGitChangedPaths {
    param([Parameter(Mandatory = $true)][string] $Root)

    $tracked = Invoke-GitLines -Root $Root -Arguments @('diff', '--name-only', '--diff-filter=ACMRTUXB', 'HEAD', '--')
    $untracked = Invoke-GitLines -Root $Root -Arguments @('ls-files', '--others', '--exclude-standard')
    return @(@($tracked) + @($untracked) |
        ForEach-Object { ConvertTo-AiwgRepoPath ([string]$_) } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Sort-Object -Unique)
}

function New-CodeXBlocker {
    param(
        [Parameter(Mandatory = $true)][string] $TaskId,
        [Parameter(Mandatory = $true)][string] $Title,
        [Parameter(Mandatory = $true)][string] $Body
    )

    $newMessageScript = Join-Path $PSScriptRoot 'New-WorkgroupMessage.ps1'
    if (-not (Test-Path -LiteralPath $newMessageScript -PathType Leaf)) {
        return $null
    }

    $output = & $newMessageScript `
        -ProjectRoot $resolvedProjectRoot `
        -WorkgroupRelativePath $WorkgroupRelativePath `
        -Task "$TaskId-advisor-blocker" `
        -From Orchestrator `
        -To CodeX `
        -Type blocker `
        -Status ready `
        -Priority high `
        -ReplyTo $TaskId `
        -RequiresHuman:$false `
        -CanWrite:$false `
        -Title $Title `
        -Body $Body `
        -Json 2>&1
    if ($? -and -not [string]::IsNullOrWhiteSpace([string]$output)) {
        return ($output | ConvertFrom-Json)
    }
    return $null
}

function Release-Lock {
    param(
        [string] $LockPath,
        [string] $TaskId
    )
    if (-not [string]::IsNullOrWhiteSpace($LockPath) -and (Test-Path -LiteralPath $LockPath -PathType Leaf)) {
        Remove-Item -LiteralPath $LockPath -Force
        Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent $agent -Type 'lock_released' -MessageId $TaskId -Path $LockPath
    }
}

$agent = 'Claude-Code'
$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
$workgroupRoot = Join-Path $resolvedProjectRoot $WorkgroupRelativePath
$inbox = Join-Path $workgroupRoot "inbox/$agent"
$done = Join-Path $workgroupRoot 'done'
$reportDir = Join-Path $workgroupRoot 'inbox/CodeX'
$diagnosticsDir = Join-Path $workgroupRoot 'state/diagnostics'
$claimScript = Join-Path $PSScriptRoot 'claim-task.ps1'
$validatorScript = Join-Path $PSScriptRoot 'validate-message.ps1'

New-Item -ItemType Directory -Force -Path $done, $reportDir, $diagnosticsDir | Out-Null

$candidate = Get-CandidateMessage -InboxPath $inbox -WantedMessageId $MessageId
if ($null -eq $candidate) {
    $result = [pscustomobject]@{
        ok = $true
        action = 'no_ready_advisory'
        message_id = $MessageId
        path = ''
    }
    Write-Result $result
    exit 0
}

$candidatePath = $candidate.File.FullName
$candidateId = $candidate.Id
if ([string]::IsNullOrWhiteSpace($candidateId)) {
    throw "Message id is missing in $candidatePath"
}

$validation = & $validatorScript -Path $candidatePath 2>&1
if (-not $?) {
    $result = [pscustomobject]@{
        ok = $false
        action = 'validate_candidate'
        message_id = $candidateId
        path = $candidatePath
        reason = ($validation -join "`n")
    }
    Write-Result $result
    exit 3
}

$frontMatter = $candidate.FrontMatter
$taskName = [string](Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'task' -Default $candidateId)
$requiresHuman = ConvertTo-AiwgBool (Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'requires_human' -Default 'false')
$canWrite = ConvertTo-AiwgBool (Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'can_write' -Default 'false')
$type = [string](Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'type' -Default '')
$contextFiles = ConvertTo-AiwgList (Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'context_files' -Default @())
$forbiddenFiles = ConvertTo-AiwgList (Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'forbidden_files' -Default @())
$timeoutMinutesValue = [string](Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'timeout_minutes' -Default '')
if (-not [string]::IsNullOrWhiteSpace($timeoutMinutesValue)) {
    $timeoutFromTask = 0
    if ([int]::TryParse($timeoutMinutesValue, [ref]$timeoutFromTask) -and $timeoutFromTask -gt 0) {
        $TimeoutSeconds = [Math]::Min($TimeoutSeconds, $timeoutFromTask * 60)
    }
}

if ($requiresHuman -or $canWrite -or $type -ne 'advisory') {
    $result = [pscustomobject]@{
        ok = $false
        action = 'advisor_guard'
        message_id = $candidateId
        path = $candidatePath
        reason = "requires_human=$requiresHuman can_write=$canWrite type=$type"
    }
    Write-Result $result
    exit 5
}

if ($DryRun) {
    $result = [pscustomobject]@{
        ok = $true
        action = 'dry_run'
        message_id = $candidateId
        path = $candidatePath
        would_call_claude = $true
        context_files = @($contextFiles)
    }
    Write-Result $result
    exit 0
}

$runnerId = "$env:COMPUTERNAME-$PID"
$claimOutput = & $claimScript -Agent $agent -WorkgroupRoot $workgroupRoot -MessageId $candidateId -RunnerId $runnerId -Json
$claimExitCode = $LASTEXITCODE
if ($claimExitCode -ne 0) {
    Write-Output $claimOutput
    exit $claimExitCode
}

$claim = $claimOutput | ConvertFrom-Json
if (-not $claim.claimed) {
    $result = [pscustomobject]@{
        ok = $true
        action = 'not_claimed'
        message_id = $candidateId
        path = $candidatePath
        reason = $claim.reason
    }
    Write-Result $result
    exit 0
}

$workingPath = (Resolve-Path -LiteralPath $claim.working_path).ProviderPath
$lockPath = (Resolve-Path -LiteralPath $claim.lock_path).ProviderPath
$taskBody = [System.IO.File]::ReadAllText($workingPath, [System.Text.Encoding]::UTF8)
$baselineChangedPaths = Get-CurrentGitChangedPaths -Root $resolvedProjectRoot
$taskFileForPrompt = $workingPath -replace '\\', '/'
$contextForPrompt = Format-ListForPrompt $contextFiles
$forbiddenForPrompt = Format-ListForPrompt $forbiddenFiles

$prompt = @"
You are Claude Code acting only as a Decision Advisor in an AI workgroup.

Task file:
$taskFileForPrompt

Context files requested by Tech Lead:
$contextForPrompt

Forbidden areas:
$forbiddenForPrompt

Hard rules:
- Advisory only. Do not edit files. Do not create files. Do not stage, commit, push, deploy, restart services, run migrations, write secrets, or modify any production environment.
- You may read files and run read-only inspection commands if needed.
- For any US production host or production environment, only read-only reasoning is allowed; do not execute any write, restart, deploy, migration, or configuration command.
- Provide 2-3 options, risks, recommendation, evidence, and validation suggestions.
- Keep the final answer in Simplified Chinese. Keep code symbols, file paths, commands, status values, and API names in English.
- If the question is under-specified, state the safest assumption and recommend a bounded next step. Do not block unless the issue would require production writes or irreversible action.

Task body:
$taskBody
"@

Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent $agent -Type 'advisor_started' -MessageId $candidateId -Path $workingPath

$job = Start-Job -ScriptBlock {
    param(
        [string] $Root,
        [string] $Prompt,
        [decimal] $Budget
    )
    Set-Location $Root
    $commandOutput = $Prompt | claude -p --max-budget-usd $Budget --permission-mode plan --tools "Read,Bash" 2>&1
    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output = ($commandOutput -join "`n")
    }
} -ArgumentList $resolvedProjectRoot, $prompt, $MaxBudgetUsd

$finished = Wait-Job -Job $job -Timeout $TimeoutSeconds
if ($null -eq $finished) {
    Stop-Job -Job $job
    Remove-Job -Job $job
    Set-AiwgFrontMatterValue -FilePath $workingPath -Key 'status' -Value 'failed'
    Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent $agent -Type 'advisor_failed' -MessageId $candidateId -Path $workingPath -Status "timeout_${TimeoutSeconds}s"
    New-CodeXBlocker -TaskId $candidateId -Title 'Claude advisory timed out' -Body "Claude advisory timed out after $TimeoutSeconds seconds. The task remains in working state for CodeX inspection." | Out-Null
    Release-Lock -LockPath $lockPath -TaskId $candidateId
    throw "Claude advisory timed out after $TimeoutSeconds seconds."
}

$commandResult = Receive-Job -Job $job
Remove-Job -Job $job

$claudeOutput = [string]$commandResult.Output
$exitCode = [int]$commandResult.ExitCode
if ($exitCode -ne 0) {
    $diagnosticPath = Join-Path $diagnosticsDir "$candidateId.advisor-output.txt"
    [System.IO.File]::WriteAllText($diagnosticPath, $claudeOutput, [System.Text.UTF8Encoding]::new($false))
    Set-AiwgFrontMatterValue -FilePath $workingPath -Key 'status' -Value 'failed'
    Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent $agent -Type 'advisor_failed' -MessageId $candidateId -Path $workingPath -Status "claude_exit_$exitCode diagnostic=$diagnosticPath"
    New-CodeXBlocker -TaskId $candidateId -Title 'Claude advisory failed' -Body "Claude advisory exited with code $exitCode. Diagnostic output: $diagnosticPath" | Out-Null
    Release-Lock -LockPath $lockPath -TaskId $candidateId
    throw "Claude advisory failed with exit code $exitCode. Diagnostic: $diagnosticPath"
}

$afterChangedPaths = Get-CurrentGitChangedPaths -Root $resolvedProjectRoot
$newChangedPaths = @($afterChangedPaths | Where-Object { $_ -notin $baselineChangedPaths })
if ($newChangedPaths.Count -gt 0) {
    Set-AiwgFrontMatterValue -FilePath $workingPath -Key 'status' -Value 'needs_manual_recovery'
    $violationText = ($newChangedPaths -join ', ')
    Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent $agent -Type 'advisor_scope_violation' -MessageId $candidateId -Path $workingPath -Status $violationText
    New-CodeXBlocker -TaskId $candidateId -Title 'Claude advisory changed files' -Body "Advisor mode is read-only, but changed paths appeared: $violationText. The task is marked needs_manual_recovery." | Out-Null
    $result = [pscustomobject]@{
        ok = $false
        action = 'advisor_scope_violation'
        message_id = $candidateId
        path = $workingPath
        reason = $violationText
    }
    Write-Result $result
    exit 6
}

$reportId = "$candidateId-advisory-report-$((Get-Date).ToString('yyyyMMddHHmmss'))"
$reportFile = "$(Get-Date -Format 'yyyy-MM-ddTHHmmss')_from-Claude-Code_to-CodeX_type-advisory_report_task-$(ConvertTo-SafeFileName $taskName).md"
$reportPath = Join-Path $reportDir $reportFile
$createdAt = New-AiwgIsoTimestamp
$taskFileRel = ConvertTo-AiwgRepoPath ($workingPath.Substring($resolvedProjectRoot.Length).TrimStart('\', '/'))

$reportBody = @"
# Claude Decision Advisor 报告：$taskName

## 原始 advisory 任务

- task_id: `$candidateId`
- task_file: `$taskFileRel`

## Advisor 输出

$claudeOutput

## Runner 说明

- 本任务以只读 advisory 模式运行。
- Runner 在执行前后检查 git changed paths，未发现新增变更。
- 最终决策权仍属于 Tech Lead / Planner。
"@

$reportContent = @"
---
id: $reportId
task: $taskName
from: Claude-Code
to: CodeX
type: advisory_report
status: ready
priority: medium
reply_to: "$candidateId"
requires_human: false
created_at: $createdAt
can_write: false
context_files:
  - $taskFileRel
allowed_files: []
forbidden_files:
  - .env
  - migrations/**
  - docs/ai-workgroup/state/**
attempt: 0
max_attempts: 1
timeout_minutes: 30
review_delegate: CodeX
---

$reportBody
"@

[System.IO.File]::WriteAllText($reportPath, $reportContent, [System.Text.UTF8Encoding]::new($false))
$reportValidation = & $validatorScript -Path $reportPath 2>&1
if (-not $?) {
    Set-AiwgFrontMatterValue -FilePath $workingPath -Key 'status' -Value 'failed'
    Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent $agent -Type 'advisor_failed' -MessageId $candidateId -Path $workingPath -Status "invalid_report: $($reportValidation -join '; ')"
    New-CodeXBlocker -TaskId $candidateId -Title 'Claude advisory report invalid' -Body "Advisor report failed protocol validation: $($reportValidation -join '; ')" | Out-Null
    Release-Lock -LockPath $lockPath -TaskId $candidateId
    throw "Claude advisory report failed validation: $($reportValidation -join "`n")"
}

Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent $agent -Type 'advisory_report_written' -MessageId $candidateId -Path $reportPath

$donePath = Join-Path $done (Split-Path $workingPath -Leaf)
Set-AiwgFrontMatterValue -FilePath $workingPath -Key 'status' -Value 'done'
Set-AiwgFrontMatterValue -FilePath $workingPath -Key 'completed_at' -Value (New-AiwgIsoTimestamp)
Move-Item -LiteralPath $workingPath -Destination $donePath
Release-Lock -LockPath $lockPath -TaskId $candidateId
Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent $agent -Type 'advisory_task_done' -MessageId $candidateId -Path $donePath -Status 'ok'

$result = [pscustomobject]@{
    ok = $true
    action = 'advised'
    message_id = $candidateId
    path = $donePath
    report_path = $reportPath
}
Write-Result $result
exit 0
