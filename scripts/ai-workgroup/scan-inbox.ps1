param(
    [string] $WorkgroupRoot = 'docs/ai-workgroup',
    [string[]] $Agents = @('Fake', 'OpenCode', 'Claude-Code'),
    [int] $MaxMessages = 10,
    [string] $PolicyPath = '',
    [switch] $AllowExternalAgents,
    [switch] $DryRun,
    [switch] $Json
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

function Add-ContentWithRetry {
    param(
        [string] $Path,
        [string] $Value,
        [int] $Attempts = 10
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            Add-Content -LiteralPath $Path -Value $Value -Encoding UTF8 -ErrorAction Stop
            return
        } catch {
            if ($attempt -eq $Attempts) {
                throw
            }
            Start-Sleep -Milliseconds (50 * $attempt)
        }
    }
}

function Write-OrchestratorEvent {
    param(
        [string] $Type,
        [string] $Agent = '',
        [string] $MessageId = '',
        [string] $Path = '',
        [string] $Status = ''
    )

    $event = [ordered]@{
        type = $Type
        agent = 'Orchestrator'
        at = (New-IsoTimestamp)
    }
    if (-not [string]::IsNullOrWhiteSpace($Agent)) {
        $event.target_agent = $Agent
    }
    if (-not [string]::IsNullOrWhiteSpace($MessageId)) {
        $event.message_id = $MessageId
    }
    if (-not [string]::IsNullOrWhiteSpace($Path)) {
        $event.path = $Path
    }
    if (-not [string]::IsNullOrWhiteSpace($Status)) {
        $event.status = $Status
    }

    Add-ContentWithRetry -Path $eventsPath -Value ($event | ConvertTo-Json -Compress)
}

function New-Result {
    param(
        [string] $Agent,
        [string] $MessageId,
        [string] $Path,
        [string] $Action,
        [string] $Status,
        [int] $ExitCode = 0
    )

    return [pscustomobject]@{
        agent = $Agent
        message_id = $MessageId
        path = $Path
        action = $Action
        status = $Status
        exit_code = $ExitCode
    }
}

function Invoke-Validator {
    param([string] $Path)

    $output = & 'scripts/ai-workgroup/validate-message.ps1' -Path $Path 2>&1
    $ok = $?
    return [pscustomobject]@{
        ExitCode = $(if ($ok) { 0 } else { 1 })
        Output = ($output -join "`n")
    }
}

function Invoke-AgentRunner {
    param(
        [string] $Agent,
        [string] $MessageId,
        [string] $MessagePath
    )

    if ($Agent -eq 'Fake') {
        return & 'scripts/ai-workgroup/fake-runner.ps1' -Agent $Agent -WorkgroupRoot $WorkgroupRoot 2>&1
    }

    if ($Agent -eq 'OpenCode') {
        if (-not $AllowExternalAgents) {
            throw "External agent dispatch is disabled. Re-run with -AllowExternalAgents to call OpenCode."
        }
        $policy = Invoke-RunnerPolicy -Agent $Agent -MessageId $MessageId -MessagePath $MessagePath
        if (-not $policy.allowed) {
            throw "Runner policy denied $Agent for ${MessageId}: $($policy.reasons -join ',')"
        }
        return & 'scripts/ai-workgroup/opencode-headless-runner.ps1' -Agent $Agent -WorkgroupRoot $WorkgroupRoot -MessageId $MessageId -TimeoutSeconds ([int]$policy.timeout_seconds) 2>&1
    }

    if ($Agent -eq 'Claude-Code') {
        if (-not $AllowExternalAgents) {
            throw "External agent dispatch is disabled. Re-run with -AllowExternalAgents to call Claude-Code."
        }
        $policy = Invoke-RunnerPolicy -Agent $Agent -MessageId $MessageId -MessagePath $MessagePath
        if (-not $policy.allowed) {
            throw "Runner policy denied $Agent for ${MessageId}: $($policy.reasons -join ',')"
        }
        return & 'scripts/ai-workgroup/claude-headless-runner.ps1' -Agent $Agent -WorkgroupRoot $WorkgroupRoot -MessageId $MessageId -TimeoutSeconds ([int]$policy.timeout_seconds) -MaxBudgetUsd ([decimal]$policy.max_budget_usd) 2>&1
    }

    throw "No runner is configured for agent '$Agent'."
}

function Invoke-RunnerPolicy {
    param(
        [string] $Agent,
        [string] $MessageId,
        [string] $MessagePath
    )

    $policyArgs = @{
        Agent = $Agent
        MessagePath = $MessagePath
        WorkgroupRoot = $WorkgroupRoot
        Record = $true
        Json = $true
    }
    if (-not [string]::IsNullOrWhiteSpace($PolicyPath)) {
        $policyArgs.PolicyPath = $PolicyPath
    }

    $output = & 'scripts/ai-workgroup/check-runner-policy.ps1' @policyArgs
    if (-not $?) {
        throw "Runner policy check failed for $Agent / $MessageId`: $($output -join "`n")"
    }
    $policy = $output | ConvertFrom-Json
    Write-OrchestratorEvent -Type 'runner_policy_checked' -Agent $Agent -MessageId $MessageId -Path $MessagePath -Status "allowed=$($policy.allowed) used=$($policy.used_today)/$($policy.daily_limit)"
    return $policy
}

$eventsPath = Join-Path $WorkgroupRoot 'state/events.Orchestrator.jsonl'
New-Item -ItemType Directory -Force -Path (Split-Path $eventsPath -Parent) | Out-Null

$results = New-Object System.Collections.ArrayList
$processed = 0

Write-OrchestratorEvent -Type 'scan_started' -Status "dry_run=$DryRun allow_external=$AllowExternalAgents"

foreach ($agent in $Agents) {
    if ($processed -ge $MaxMessages) {
        break
    }

    $inbox = Join-Path $WorkgroupRoot "inbox/$agent"
    if (-not (Test-Path -LiteralPath $inbox -PathType Container)) {
        [void] $results.Add((New-Result -Agent $agent -MessageId '' -Path $inbox -Action 'skip' -Status 'missing_inbox'))
        continue
    }

    $messages = Get-ChildItem -LiteralPath $inbox -Filter '*.md' -File -ErrorAction SilentlyContinue | Sort-Object Name
    foreach ($message in $messages) {
        if ($processed -ge $MaxMessages) {
            break
        }

        $lines = [System.IO.File]::ReadAllLines($message.FullName, [System.Text.Encoding]::UTF8)
        $messageId = Read-FrontMatterValue -Lines $lines -Key 'id'
        $status = Read-FrontMatterValue -Lines $lines -Key 'status'
        $to = Read-FrontMatterValue -Lines $lines -Key 'to'
        $requiresHuman = Read-FrontMatterValue -Lines $lines -Key 'requires_human'

        if ($status -ne 'ready') {
            [void] $results.Add((New-Result -Agent $agent -MessageId $messageId -Path $message.FullName -Action 'skip' -Status "status_$status"))
            continue
        }

        if ($to -ne $agent) {
            [void] $results.Add((New-Result -Agent $agent -MessageId $messageId -Path $message.FullName -Action 'skip' -Status "to_$to"))
            continue
        }

        if ($requiresHuman -eq 'true') {
            Write-OrchestratorEvent -Type 'message_human_gated' -Agent $agent -MessageId $messageId -Path $message.FullName -Status 'requires_human'
            [void] $results.Add((New-Result -Agent $agent -MessageId $messageId -Path $message.FullName -Action 'skip' -Status 'requires_human'))
            continue
        }

        $validation = Invoke-Validator -Path $message.FullName
        if ($validation.ExitCode -ne 0) {
            Write-OrchestratorEvent -Type 'message_invalid' -Agent $agent -MessageId $messageId -Path $message.FullName -Status $validation.Output
            [void] $results.Add((New-Result -Agent $agent -MessageId $messageId -Path $message.FullName -Action 'skip' -Status 'invalid_message' -ExitCode $validation.ExitCode))
            continue
        }

        Write-OrchestratorEvent -Type 'message_ready' -Agent $agent -MessageId $messageId -Path $message.FullName

        if ($DryRun) {
            [void] $results.Add((New-Result -Agent $agent -MessageId $messageId -Path $message.FullName -Action 'would_dispatch' -Status 'ready'))
            $processed++
            continue
        }

        try {
            Write-OrchestratorEvent -Type 'dispatch_started' -Agent $agent -MessageId $messageId -Path $message.FullName
            $runnerOutput = Invoke-AgentRunner -Agent $agent -MessageId $messageId -MessagePath $message.FullName
            $runnerOk = $?
            $runnerExit = if ($runnerOk) { 0 } else { 1 }
            if ($runnerExit -ne 0) {
                Write-OrchestratorEvent -Type 'dispatch_failed' -Agent $agent -MessageId $messageId -Path $message.FullName -Status "exit_$runnerExit"
                [void] $results.Add((New-Result -Agent $agent -MessageId $messageId -Path $message.FullName -Action 'dispatch_failed' -Status (($runnerOutput -join "`n")) -ExitCode $runnerExit))
                continue
            }
            Write-OrchestratorEvent -Type 'dispatch_finished' -Agent $agent -MessageId $messageId -Path $message.FullName -Status 'ok'
            [void] $results.Add((New-Result -Agent $agent -MessageId $messageId -Path $message.FullName -Action 'dispatched' -Status (($runnerOutput -join "`n"))))
            $processed++
        } catch {
            Write-OrchestratorEvent -Type 'dispatch_failed' -Agent $agent -MessageId $messageId -Path $message.FullName -Status $_.Exception.Message
            [void] $results.Add((New-Result -Agent $agent -MessageId $messageId -Path $message.FullName -Action 'dispatch_failed' -Status $_.Exception.Message -ExitCode 1))
            $processed++
        }
    }
}

Write-OrchestratorEvent -Type 'scan_finished' -Status "processed=$processed"

if ($Json) {
    if ($results.Count -eq 0) {
        Write-Output '[]'
    } else {
        @($results) | ConvertTo-Json -Depth 6
    }
} else {
    @($results) | Format-Table -AutoSize
}
