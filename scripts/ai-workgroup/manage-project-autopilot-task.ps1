param(
    [ValidateSet('Install', 'Uninstall', 'Status', 'Start', 'Stop', 'Enable', 'Disable', 'Command')]
    [string] $Action = 'Status',
    [string] $TaskName = 'AIWG-Project-Autopilot-AIVideoTrans',
    [string] $TaskPath = '\AIWorkgroup\',
    [string] $OrchestratorRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).ProviderPath,
    [string] $ProjectRoot = 'D:\example\protected-business-repo',
    [string] $WorkgroupRelativePath = 'docs/ai-workgroup',
    [int] $EveryMinutes = 10,
    [decimal] $MaxBudgetUsd = 3.00,
    [int] $TimeoutSeconds = 1200,
    [switch] $AllowWrite,
    [switch] $Disabled,
    [switch] $DryRun,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'

function ConvertTo-CommandLineArgument {
    param([string] $Value)

    if ($null -eq $Value) {
        return '""'
    }
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + ($Value -replace '"', '\"') + '"'
}

function New-AutopilotCommand {
    param(
        [string] $Root,
        [string] $TargetProject,
        [string] $GroupRelativePath,
        [decimal] $Budget,
        [int] $Timeout,
        [bool] $EnableWrite
    )

    $scriptPath = Join-Path $Root 'scripts/ai-workgroup/Invoke-ProjectAutopilotScheduled.ps1'
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Invoke-ProjectAutopilotScheduled.ps1 was not found at $scriptPath"
    }

    $args = New-Object System.Collections.ArrayList
    [void] $args.Add('-NoProfile')
    [void] $args.Add('-ExecutionPolicy')
    [void] $args.Add('Bypass')
    [void] $args.Add('-File')
    [void] $args.Add($scriptPath)
    [void] $args.Add('-ProjectRoot')
    [void] $args.Add($TargetProject)
    [void] $args.Add('-WorkgroupRelativePath')
    [void] $args.Add($GroupRelativePath)
    [void] $args.Add('-MaxBudgetUsd')
    [void] $args.Add([string]$Budget)
    [void] $args.Add('-TimeoutSeconds')
    [void] $args.Add([string]$Timeout)
    if ($EnableWrite) {
        [void] $args.Add('-AllowWrite')
    }

    return [pscustomobject]@{
        Execute = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
        Argument = ((@($args) | ForEach-Object { ConvertTo-CommandLineArgument $_ }) -join ' ')
        WorkingDirectory = $Root
    }
}

function Get-TaskOrNull {
    param(
        [string] $Name,
        [string] $Path
    )
    return Get-ScheduledTask -TaskName $Name -TaskPath $Path -ErrorAction SilentlyContinue
}

if ($EveryMinutes -lt 1) {
    throw 'EveryMinutes must be >= 1.'
}

$resolvedOrchestratorRoot = (Resolve-Path -LiteralPath $OrchestratorRoot).ProviderPath
$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
$command = New-AutopilotCommand -Root $resolvedOrchestratorRoot -TargetProject $resolvedProjectRoot -GroupRelativePath $WorkgroupRelativePath -Budget $MaxBudgetUsd -Timeout $TimeoutSeconds -EnableWrite ([bool]$AllowWrite)
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

switch ($Action) {
    'Command' {
        [pscustomobject]@{
            task_name = $TaskName
            task_path = $TaskPath
            execute = $command.Execute
            argument = $command.Argument
            working_directory = $command.WorkingDirectory
            user = $userId
            project_root = $resolvedProjectRoot
            every_minutes = $EveryMinutes
            allow_write = [bool]$AllowWrite
            max_budget_usd = $MaxBudgetUsd
            timeout_seconds = $TimeoutSeconds
        }
        break
    }

    'Status' {
        $task = Get-TaskOrNull -Name $TaskName -Path $TaskPath
        if ($null -eq $task) {
            [pscustomobject]@{
                exists = $false
                task_name = $TaskName
                task_path = $TaskPath
            }
            break
        }

        $info = Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath $TaskPath
        [pscustomobject]@{
            exists = $true
            task_name = $TaskName
            task_path = $TaskPath
            state = $task.State
            last_run_time = $info.LastRunTime
            last_task_result = $info.LastTaskResult
            next_run_time = $info.NextRunTime
            execute = $task.Actions[0].Execute
            arguments = $task.Actions[0].Arguments
            working_directory = $task.Actions[0].WorkingDirectory
        }
        break
    }

    'Install' {
        if (-not $Force -and (Get-TaskOrNull -Name $TaskName -Path $TaskPath)) {
            throw "Scheduled task already exists. Re-run with -Force to replace it."
        }

        if ($DryRun) {
            [pscustomobject]@{
                dry_run = $true
                action = 'Install'
                task_name = $TaskName
                task_path = $TaskPath
                execute = $command.Execute
                argument = $command.Argument
                working_directory = $command.WorkingDirectory
                user = $userId
                disabled = [bool]$Disabled
            }
            break
        }

        $taskAction = New-ScheduledTaskAction -Execute $command.Execute -Argument $command.Argument -WorkingDirectory $command.WorkingDirectory
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
        $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

        Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Description 'AI Workgroup project autopilot one-shot dispatcher' -Force:$Force | Out-Null
        if ($Disabled) {
            Disable-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath | Out-Null
        }
        & $PSCommandPath -Action Status -TaskName $TaskName -TaskPath $TaskPath -OrchestratorRoot $resolvedOrchestratorRoot -ProjectRoot $resolvedProjectRoot
        break
    }

    'Uninstall' {
        if ($DryRun) {
            [pscustomobject]@{
                dry_run = $true
                action = 'Uninstall'
                task_name = $TaskName
                task_path = $TaskPath
            }
            break
        }
        if (Get-TaskOrNull -Name $TaskName -Path $TaskPath) {
            Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false
        }
        [pscustomobject]@{
            exists = $false
            task_name = $TaskName
            task_path = $TaskPath
        }
        break
    }

    'Start' {
        if ($DryRun) {
            [pscustomobject]@{
                dry_run = $true
                action = 'Start'
                task_name = $TaskName
                task_path = $TaskPath
            }
            break
        }
        Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
        Start-Sleep -Seconds 1
        & $PSCommandPath -Action Status -TaskName $TaskName -TaskPath $TaskPath -OrchestratorRoot $resolvedOrchestratorRoot -ProjectRoot $resolvedProjectRoot
        break
    }

    'Stop' {
        if ($DryRun) {
            [pscustomobject]@{
                dry_run = $true
                action = 'Stop'
                task_name = $TaskName
                task_path = $TaskPath
            }
            break
        }
        Stop-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
        Start-Sleep -Seconds 1
        & $PSCommandPath -Action Status -TaskName $TaskName -TaskPath $TaskPath -OrchestratorRoot $resolvedOrchestratorRoot -ProjectRoot $resolvedProjectRoot
        break
    }

    'Enable' {
        if ($DryRun) {
            [pscustomobject]@{
                dry_run = $true
                action = 'Enable'
                task_name = $TaskName
                task_path = $TaskPath
            }
            break
        }
        Enable-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath | Out-Null
        & $PSCommandPath -Action Status -TaskName $TaskName -TaskPath $TaskPath -OrchestratorRoot $resolvedOrchestratorRoot -ProjectRoot $resolvedProjectRoot
        break
    }

    'Disable' {
        if ($DryRun) {
            [pscustomobject]@{
                dry_run = $true
                action = 'Disable'
                task_name = $TaskName
                task_path = $TaskPath
            }
            break
        }
        Disable-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath | Out-Null
        & $PSCommandPath -Action Status -TaskName $TaskName -TaskPath $TaskPath -OrchestratorRoot $resolvedOrchestratorRoot -ProjectRoot $resolvedProjectRoot
        break
    }
}
