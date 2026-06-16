param(
    [string] $WorkgroupRoot = 'docs/ai-workgroup',
    [string[]] $Agents = @('Fake', 'OpenCode', 'Claude-Code'),
    [int] $PollSeconds = 1800,
    [int] $DebounceMilliseconds = 750,
    [int] $StaleMinutes = 120,
    [int] $StopAfterSeconds = 0,
    [switch] $AllowExternalAgents,
    [switch] $DryRun,
    [switch] $SkipInitialScan,
    [switch] $SkipStaleCheck,
    [switch] $PollOnly,
    [switch] $Json
)

$ErrorActionPreference = 'Stop'

function New-IsoTimestamp {
    return (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
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

function Write-WatchEvent {
    param(
        [string] $Type,
        [string] $Status = ''
    )

    $event = [ordered]@{
        type = $Type
        agent = 'Orchestrator'
        at = (New-IsoTimestamp)
    }
    if (-not [string]::IsNullOrWhiteSpace($Status)) {
        $event.status = $Status
    }
    Add-ContentWithRetry -Path $eventsPath -Value ($event | ConvertTo-Json -Compress)
}

function Write-Heartbeat {
    param([string] $Status = 'ok')

    $heartbeat = [ordered]@{
        agent = 'Orchestrator'
        pid = $PID
        at = (New-IsoTimestamp)
        status = $Status
    }
    Add-ContentWithRetry -Path $heartbeatPath -Value ($heartbeat | ConvertTo-Json -Compress)
}

function Invoke-Scanner {
    param([string] $Reason)

    Write-Heartbeat -Status "scan:$Reason"
    Write-WatchEvent -Type 'watch_scan_started' -Status $Reason

    $scanParams = @{
        WorkgroupRoot = $WorkgroupRoot
        Agents = $Agents
        Json = $true
    }
    if ($AllowExternalAgents) {
        $scanParams.AllowExternalAgents = $true
    }
    if ($DryRun) {
        $scanParams.DryRun = $true
    }

    $output = & 'scripts/ai-workgroup/scan-inbox.ps1' @scanParams 2>&1
    $ok = $?
    if ($ok) {
        Write-WatchEvent -Type 'watch_scan_finished' -Status $Reason
    } else {
        Write-WatchEvent -Type 'watch_scan_failed' -Status ($output -join "`n")
    }

    if ($Json) {
        $output
    } elseif (-not $ok) {
        Write-Warning ($output -join "`n")
    } else {
        $output
    }

    if (-not $SkipStaleCheck) {
        Invoke-StaleCheck -Reason $Reason
    }
}

function Invoke-StaleCheck {
    param([string] $Reason)

    Write-Heartbeat -Status "stale:$Reason"
    Write-WatchEvent -Type 'watch_stale_check_started' -Status $Reason

    $staleParams = @{
        WorkgroupRoot = $WorkgroupRoot
        Agents = $Agents
        StaleMinutes = $StaleMinutes
        Json = $true
    }
    if ($DryRun) {
        $staleParams.DryRun = $true
    }

    $output = & 'scripts/ai-workgroup/check-stale-claims.ps1' @staleParams 2>&1
    $ok = $?
    if ($ok) {
        Write-WatchEvent -Type 'watch_stale_check_finished' -Status $Reason
    } else {
        Write-WatchEvent -Type 'watch_stale_check_failed' -Status ($output -join "`n")
    }

    if ($Json) {
        $output
    } elseif (-not $ok) {
        Write-Warning ($output -join "`n")
    } else {
        $output
    }
}

if ($PollSeconds -lt 1) {
    throw 'PollSeconds must be at least 1.'
}
if ($DebounceMilliseconds -lt 0) {
    throw 'DebounceMilliseconds must be non-negative.'
}

$stateDir = Join-Path $WorkgroupRoot 'state'
$eventsPath = Join-Path $stateDir 'events.Orchestrator.jsonl'
$heartbeatPath = Join-Path $stateDir 'heartbeats.Orchestrator.jsonl'
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

$sourcePrefix = "AIWG-Watch-$PID"
$sourceIdentifiers = New-Object System.Collections.ArrayList
$watchers = New-Object System.Collections.ArrayList

Write-Heartbeat -Status 'started'
Write-WatchEvent -Type 'watch_started' -Status "agents=$($Agents -join ',') poll_seconds=$PollSeconds stale_minutes=$StaleMinutes"

try {
    if (-not $PollOnly) {
        foreach ($agent in $Agents) {
            $inbox = Join-Path $WorkgroupRoot "inbox/$agent"
            New-Item -ItemType Directory -Force -Path $inbox | Out-Null

            $watcher = New-Object System.IO.FileSystemWatcher
            $watcher.Path = (Resolve-Path -LiteralPath $inbox).ProviderPath
            $watcher.Filter = '*.md'
            $watcher.IncludeSubdirectories = $false
            $watcher.EnableRaisingEvents = $true
            [void] $watchers.Add($watcher)

            foreach ($eventName in @('Created', 'Renamed', 'Changed')) {
                $sourceId = "$sourcePrefix-$agent-$eventName"
                Register-ObjectEvent -InputObject $watcher -EventName $eventName -SourceIdentifier $sourceId | Out-Null
                [void] $sourceIdentifiers.Add($sourceId)
            }
        }
    }

    if (-not $SkipInitialScan) {
        Invoke-Scanner -Reason 'initial'
    }

    $startedAt = Get-Date
    while ($true) {
        if ($StopAfterSeconds -gt 0 -and ((Get-Date) - $startedAt).TotalSeconds -ge $StopAfterSeconds) {
            break
        }

        $timeout = $PollSeconds
        if ($StopAfterSeconds -gt 0) {
            $remaining = [math]::Ceiling($StopAfterSeconds - ((Get-Date) - $startedAt).TotalSeconds)
            if ($remaining -le 0) {
                break
            }
            $timeout = [math]::Min($PollSeconds, [int]$remaining)
        }

        if ($PollOnly) {
            Start-Sleep -Seconds $timeout
            Invoke-Scanner -Reason 'poll'
            continue
        }

        $event = Wait-Event -Timeout $timeout
        if ($null -eq $event) {
            Invoke-Scanner -Reason 'poll'
            continue
        }

        $matched = $false
        foreach ($queuedEvent in @(Get-Event)) {
            if ($queuedEvent.SourceIdentifier -like "$sourcePrefix-*") {
                $matched = $true
                Remove-Event -EventIdentifier $queuedEvent.EventIdentifier
            }
        }

        if ($matched) {
            if ($DebounceMilliseconds -gt 0) {
                Start-Sleep -Milliseconds $DebounceMilliseconds
            }
            Invoke-Scanner -Reason 'filesystem'
        }
    }
} finally {
    foreach ($sourceId in @($sourceIdentifiers)) {
        Unregister-Event -SourceIdentifier $sourceId -ErrorAction SilentlyContinue
    }
    foreach ($queuedEvent in @(Get-Event)) {
        if ($queuedEvent.SourceIdentifier -like "$sourcePrefix-*") {
            Remove-Event -EventIdentifier $queuedEvent.EventIdentifier -ErrorAction SilentlyContinue
        }
    }
    foreach ($watcher in @($watchers)) {
        $watcher.EnableRaisingEvents = $false
        $watcher.Dispose()
    }
    Write-Heartbeat -Status 'stopped'
    Write-WatchEvent -Type 'watch_stopped'
}
