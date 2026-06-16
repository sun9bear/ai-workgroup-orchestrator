param(
    [ValidateSet('Install', 'Uninstall', 'Status', 'Start', 'Stop', 'Command')]
    [string] $Action = 'Status',
    [string] $TaskName = 'AIWG-Orchestrator-Watcher',
    [string] $TaskPath = '\AIWorkgroup\',
    [string] $ProjectRoot = (Get-Location).Path,
    [string] $WorkgroupRoot = 'docs/ai-workgroup',
    [string[]] $Agents = @('Fake'),
    [int] $PollSeconds = 1800,
    [int] $StaleMinutes = 120,
    [switch] $AllowExternalAgents,
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

function New-WatcherCommand {
    param(
        [string] $Root,
        [string] $GroupRoot,
        [string[]] $AgentList,
        [int] $Poll,
        [int] $Stale,
        [bool] $EnableExternal
    )

    $scriptPath = Join-Path $Root 'scripts/ai-workgroup/watch-inbox.ps1'
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "watch-inbox.ps1 was not found at $scriptPath"
    }

    $args = New-Object System.Collections.ArrayList
    [void] $args.Add('-NoProfile')
    [void] $args.Add('-ExecutionPolicy')
    [void] $args.Add('Bypass')
    [void] $args.Add('-File')
    [void] $args.Add($scriptPath)
    [void] $args.Add('-WorkgroupRoot')
    [void] $args.Add($GroupRoot)
    [void] $args.Add('-Agents')
    foreach ($agent in $AgentList) {
        [void] $args.Add($agent)
    }
    [void] $args.Add('-PollSeconds')
    [void] $args.Add([string]$Poll)
    [void] $args.Add('-StaleMinutes')
    [void] $args.Add([string]$Stale)
    if ($EnableExternal) {
        [void] $args.Add('-AllowExternalAgents')
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

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
$command = New-WatcherCommand -Root $resolvedRoot -GroupRoot $WorkgroupRoot -AgentList $Agents -Poll $PollSeconds -Stale $StaleMinutes -EnableExternal ([bool]$AllowExternalAgents)
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
            agents = $Agents
            allow_external_agents = [bool]$AllowExternalAgents
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
            }
            break
        }

        $taskAction = New-ScheduledTaskAction -Execute $command.Execute -Argument $command.Argument -WorkingDirectory $command.WorkingDirectory
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
        $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

        Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Description 'AI Workgroup local inbox watcher' -Force:$Force | Out-Null
        & $PSCommandPath -Action Status -TaskName $TaskName -TaskPath $TaskPath -ProjectRoot $resolvedRoot -WorkgroupRoot $WorkgroupRoot -Agents $Agents
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
        & $PSCommandPath -Action Status -TaskName $TaskName -TaskPath $TaskPath -ProjectRoot $resolvedRoot -WorkgroupRoot $WorkgroupRoot -Agents $Agents
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
        & $PSCommandPath -Action Status -TaskName $TaskName -TaskPath $TaskPath -ProjectRoot $resolvedRoot -WorkgroupRoot $WorkgroupRoot -Agents $Agents
        break
    }
}
