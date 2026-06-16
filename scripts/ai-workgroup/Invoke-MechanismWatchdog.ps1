param(
    [string] $ProjectRoot = 'D:\example\protected-business-repo',
    [string] $WorkgroupRelativePath = 'docs/ai-workgroup',
    [string] $DashboardUrl = 'http://127.0.0.1:8765',
    [int] $StaleReadyMinutes = 10,
    [string] $LogRoot = '',
    [switch] $EnableRoleNudge,
    [int] $RoleNudgeCooldownMinutes = 15,
    [int] $RoleNudgeExpireMinutes = 15,
    [switch] $Json
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

function New-WatchdogResult {
    [pscustomobject]@{
        generated_at = New-AiwgIsoTimestamp
        project_root = ''
        dashboard_ok = $false
        summary_counts = @{}
        task_scheduler = @()
        stale_ready = @()
        stale_failed = @()
        actions = @()
        warnings = @()
        errors = @()
    }
}

function Add-ListItem {
    param(
        [Parameter(Mandatory = $true)] $List,
        [Parameter(Mandatory = $true)] $Item
    )
    [void] $List.Add($Item)
}

function Get-MessageTime {
    param($Message)
    $raw = [string]$Message.created_at
    if (-not [string]::IsNullOrWhiteSpace($raw)) {
        try {
            return [datetimeoffset]::Parse($raw)
        } catch {
            # Fall through to mtime.
        }
    }
    try {
        return [datetimeoffset]::Parse([string]$Message.mtime_iso)
    } catch {
        return [datetimeoffset]::Now
    }
}

function Get-RoleReadyMessages {
    param($Summary)
    $items = New-Object System.Collections.Generic.List[object]
    foreach ($message in @($Summary.ready)) {
        $to = [string]$message.to
        if ($to -in @('CodeX', 'Reviewer', 'Git-Steward', 'Claude-Code')) {
            [void] $items.Add($message)
        }
    }
    return $items
}

function Invoke-TaskIfPresent {
    param(
        [string] $TaskName,
        [string] $TaskPath,
        [string] $Reason,
        $Result
    )
    try {
        $task = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop
        if ($task.State -eq 'Disabled') {
            Add-ListItem -List $Result.warnings -Item "$TaskPath$TaskName is disabled; not starting."
            return
        }
        Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
        Add-ListItem -List $Result.actions -Item "started $TaskPath$TaskName ($Reason)"
    } catch {
        Add-ListItem -List $Result.errors -Item "failed to start ${TaskPath}${TaskName}: $($_.Exception.Message)"
    }
}

function Test-AutomationConfig {
    param(
        [string] $AutomationId,
        $Result
    )
    $codexHome = $env:CODEX_HOME
    if ([string]::IsNullOrWhiteSpace($codexHome)) {
        $codexHome = Join-Path $env:USERPROFILE '.codex'
    }
    $path = Join-Path $codexHome "automations/$AutomationId/automation.toml"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Add-ListItem -List $Result.warnings -Item "automation missing: $AutomationId"
        return
    }
    $raw = Get-Content -LiteralPath $path -Raw -Encoding UTF8
    if ($raw -notmatch 'status\s*=\s*"ACTIVE"') {
        Add-ListItem -List $Result.warnings -Item "automation not ACTIVE: $AutomationId"
    }
}

function Convert-AgentToNudgeRole {
    param([string] $Agent)
    switch ($Agent) {
        'CodeX' { return 'TechLead' }
        'Reviewer' { return 'Reviewer' }
        'Git-Steward' { return 'GitSteward' }
        default { return '' }
    }
}

function Invoke-RoleNudge {
    param(
        [string] $Role,
        [string] $Reason,
        [string] $Task,
        [string] $SourcePath,
        $Result
    )
    if ([string]::IsNullOrWhiteSpace($Role)) {
        return
    }
    if (-not $EnableRoleNudge) {
        Add-ListItem -List $Result.warnings -Item "role nudge disabled; not modifying Codex automation for ${Role}: $Task"
        return
    }
    $script = Join-Path $PSScriptRoot 'Send-CodexRoleNudge.ps1'
    if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
        Add-ListItem -List $Result.warnings -Item "role nudge script missing: $script"
        return
    }
    try {
        $output = & $script `
            -Action Nudge `
            -Role $Role `
            -Reason $Reason `
            -Task $Task `
            -SourcePath $SourcePath `
            -CooldownMinutes $RoleNudgeCooldownMinutes `
            -ExpireAfterMinutes $RoleNudgeExpireMinutes `
            -Json 2>&1
        if ($LASTEXITCODE -eq 0) {
            Add-ListItem -List $Result.actions -Item "nudged $Role ($Task)"
        } else {
            Add-ListItem -List $Result.warnings -Item "failed to nudge ${Role}: $($output -join ' ')"
        }
    } catch {
        Add-ListItem -List $Result.warnings -Item "failed to nudge ${Role}: $($_.Exception.Message)"
    }
}

function Invoke-RoleNudgeSweep {
    param($Result)
    if (-not $EnableRoleNudge) {
        return
    }
    $script = Join-Path $PSScriptRoot 'Send-CodexRoleNudge.ps1'
    if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
        return
    }
    try {
        $output = & $script -Action Sweep -ExpireAfterMinutes $RoleNudgeExpireMinutes -Json 2>&1
        if ($LASTEXITCODE -eq 0) {
            Add-ListItem -List $Result.actions -Item 'swept stale role nudges'
        } else {
            Add-ListItem -List $Result.warnings -Item "role nudge sweep failed: $($output -join ' ')"
        }
    } catch {
        Add-ListItem -List $Result.warnings -Item "role nudge sweep failed: $($_.Exception.Message)"
    }
}

$result = New-WatchdogResult
$lists = @('task_scheduler', 'stale_ready', 'stale_failed', 'actions', 'warnings', 'errors')
foreach ($name in $lists) {
    $result.$name = New-Object System.Collections.Generic.List[object]
}

try {
    $resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
    $result.project_root = $resolvedProjectRoot
    $workgroupRoot = Join-Path $resolvedProjectRoot $WorkgroupRelativePath
    if ([string]::IsNullOrWhiteSpace($LogRoot)) {
        $LogRoot = Join-Path $PSScriptRoot '..\..\logs'
    }
    $LogRoot = [System.IO.Path]::GetFullPath($LogRoot)
    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
    Invoke-RoleNudgeSweep -Result $result

    try {
        $summary = Invoke-RestMethod -Uri "$DashboardUrl/api/summary" -TimeoutSec 8
        $result.dashboard_ok = $true
        $result.summary_counts = $summary.counts
    } catch {
        Add-ListItem -List $result.errors -Item "dashboard unreachable: $($_.Exception.Message)"
        $dashboardScript = Join-Path $PSScriptRoot 'Start-HumanDashboard.ps1'
        if (Test-Path -LiteralPath $dashboardScript -PathType Leaf) {
            Start-Process -FilePath 'powershell.exe' -ArgumentList @(
                '-NoProfile',
                '-ExecutionPolicy',
                'Bypass',
                '-File',
                $dashboardScript,
                '-ProjectRoot',
                $resolvedProjectRoot,
                '-NoBrowser'
            ) -WindowStyle Hidden | Out-Null
            Add-ListItem -List $result.actions -Item 'started dashboard server'
        }
        $summary = $null
    }

    foreach ($taskName in @('AIWG-Claude-Implementer-AIVideoTrans', 'AIWG-Claude-Advisor-AIVideoTrans', 'AIWG-Orchestrator-Watcher', 'AIWG-Project-Autopilot-AIVideoTrans')) {
        try {
            $task = Get-ScheduledTask -TaskName $taskName -TaskPath '\AIWorkgroup\' -ErrorAction Stop
            $info = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath '\AIWorkgroup\' -ErrorAction Stop
            $item = [pscustomobject]@{
                task = "\AIWorkgroup\$taskName"
                state = [string]$task.State
                last_run_time = $info.LastRunTime
                next_run_time = $info.NextRunTime
                last_task_result = $info.LastTaskResult
            }
            Add-ListItem -List $result.task_scheduler -Item $item
            if ($taskName -in @('AIWG-Claude-Implementer-AIVideoTrans', 'AIWG-Claude-Advisor-AIVideoTrans') -and $task.State -eq 'Disabled') {
                Add-ListItem -List $result.errors -Item "$taskName is disabled"
            }
            if ($taskName -in @('AIWG-Orchestrator-Watcher', 'AIWG-Project-Autopilot-AIVideoTrans') -and $task.State -ne 'Disabled') {
                Add-ListItem -List $result.warnings -Item "$taskName should remain Disabled in the role workflow"
            }
        } catch {
            Add-ListItem -List $result.errors -Item "scheduled task missing/unreadable: $taskName"
        }
    }

    foreach ($automationId in @('aivideotrans-tech-lead-planner', 'aivideotrans-reviewer', 'aivideotrans-git-steward')) {
        Test-AutomationConfig -AutomationId $automationId -Result $result
    }

    if ($null -ne $summary) {
        $now = [datetimeoffset]::Now
        foreach ($message in @(Get-RoleReadyMessages -Summary $summary)) {
            $age = $now - (Get-MessageTime -Message $message)
            if ($age.TotalMinutes -ge $StaleReadyMinutes) {
                $stale = [pscustomobject]@{
                    task = [string]$message.task
                    to = [string]$message.to
                    age_minutes = [math]::Floor($age.TotalMinutes)
                    path = [string]$message.relative_path
                }
                Add-ListItem -List $result.stale_ready -Item $stale
                if ([string]$message.to -eq 'Claude-Code') {
                    $kind = [string]$message.type
                    if ($kind -eq 'advisory') {
                        Invoke-TaskIfPresent -TaskName 'AIWG-Claude-Advisor-AIVideoTrans' -TaskPath '\AIWorkgroup\' -Reason "stale advisory $($message.task)" -Result $result
                    } else {
                        Invoke-TaskIfPresent -TaskName 'AIWG-Claude-Implementer-AIVideoTrans' -TaskPath '\AIWorkgroup\' -Reason "stale implementation $($message.task)" -Result $result
                    }
                } else {
                    Add-ListItem -List $result.warnings -Item "$($message.to) ready task is stale: $($message.task). Watchdog will not perform that role."
                    $role = Convert-AgentToNudgeRole -Agent ([string]$message.to)
                    Invoke-RoleNudge `
                        -Role $role `
                        -Reason "$($message.to) ready task has waited for $([math]::Floor($age.TotalMinutes)) minutes; watchdog is only waking the role session." `
                        -Task ([string]$message.task) `
                        -SourcePath ([string]$message.relative_path) `
                        -Result $result
                }
            }
        }

        foreach ($message in @($summary.active)) {
            if ([string]$message.status -eq 'failed') {
                $age = $now - (Get-MessageTime -Message $message)
                Add-ListItem -List $result.stale_failed -Item ([pscustomobject]@{
                    task = [string]$message.task
                    path = [string]$message.relative_path
                    mtime_iso = [string]$message.mtime_iso
                    age_minutes = [math]::Floor($age.TotalMinutes)
                })
                if ($age.TotalMinutes -ge $StaleReadyMinutes) {
                    Invoke-RoleNudge `
                        -Role 'TechLead' `
                        -Reason "Uncovered failed workflow item has waited for $([math]::Floor($age.TotalMinutes)) minutes; watchdog is only waking Tech Lead to decide the next step." `
                        -Task ([string]$message.task) `
                        -SourcePath ([string]$message.relative_path) `
                        -Result $result
                }
            }
        }
    }

    Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent 'Watchdog' -Type 'mechanism_watchdog_checked' -Status "warnings=$($result.warnings.Count);errors=$($result.errors.Count)"
    $statusPath = Join-Path $LogRoot 'mechanism-watchdog-status.json'
    ($result | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $statusPath -Encoding UTF8

    if ($Json) {
        $result | ConvertTo-Json -Depth 8
    } else {
        Write-Output "dashboard_ok=$($result.dashboard_ok)"
        Write-Output "warnings=$($result.warnings.Count)"
        Write-Output "errors=$($result.errors.Count)"
        foreach ($warning in @($result.warnings)) {
            Write-Output "WARN $warning"
        }
        foreach ($errorItem in @($result.errors)) {
            Write-Output "ERR $errorItem"
        }
    }

    if ($result.errors.Count -gt 0) {
        exit 2
    }
    exit 0
} catch {
    Add-ListItem -List $result.errors -Item $_.Exception.Message
    if ($Json) {
        $result | ConvertTo-Json -Depth 8
    } else {
        Write-Error $_.Exception.Message
    }
    exit 1
}
