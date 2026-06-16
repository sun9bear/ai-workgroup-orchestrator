param(
    [string] $WorkgroupRoot = 'docs/ai-workgroup',
    [string[]] $Agents = @('Fake', 'OpenCode', 'Claude-Code'),
    [int] $StaleMinutes = 120,
    [string[]] $Statuses = @('claimed', 'working'),
    [switch] $DryRun,
    [switch] $Json
)

$ErrorActionPreference = 'Stop'

function New-IsoTimestamp {
    return (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
}

function New-FileTimestamp {
    return (Get-Date).ToString("yyyy-MM-ddTHHmmss")
}

function ConvertTo-SafeFileName {
    param([string] $Value)
    return ($Value -replace '[^A-Za-z0-9_.-]', '_')
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

function Test-ExistingNotification {
    param([string] $MessageId)

    if ([string]::IsNullOrWhiteSpace($MessageId) -or -not (Test-Path -LiteralPath $codeXInbox -PathType Container)) {
        return $false
    }

    $notifications = Get-ChildItem -LiteralPath $codeXInbox -Filter '*.md' -File -ErrorAction SilentlyContinue
    foreach ($notification in $notifications) {
        $lines = [System.IO.File]::ReadAllLines($notification.FullName, [System.Text.Encoding]::UTF8)
        $replyTo = Read-FrontMatterValue -Lines $lines -Key 'reply_to'
        $status = Read-FrontMatterValue -Lines $lines -Key 'status'
        $from = Read-FrontMatterValue -Lines $lines -Key 'from'
        if ($replyTo -eq $MessageId -and $status -eq 'stale_claim' -and $from -eq 'Orchestrator') {
            return $true
        }
    }
    return $false
}

function New-StaleNotification {
    param(
        [string] $Agent,
        [string] $MessageId,
        [string] $Task,
        [string] $Priority,
        [string] $WorkingPath,
        [string] $LockPath,
        [double] $AgeMinutes,
        [string] $Reason,
        [string] $Status
    )

    if ([string]::IsNullOrWhiteSpace($Task)) {
        $Task = 'unknown-task'
    }
    if ([string]::IsNullOrWhiteSpace($Priority)) {
        $Priority = 'high'
    }

    $createdAt = New-IsoTimestamp
    $fileTimestamp = New-FileTimestamp
    $safeTask = ConvertTo-SafeFileName $Task
    $safeMessageId = ConvertTo-SafeFileName $MessageId
    $notificationId = "STALE-$safeMessageId-$fileTimestamp"
    $notificationName = "${fileTimestamp}_from-Orchestrator_to-CodeX_type-blocker_task-$safeTask`_stale-claim.md"
    $notificationPath = Join-Path $codeXInbox $notificationName
    $workingForMd = $WorkingPath -replace '\\', '/'
    $lockForMd = $LockPath -replace '\\', '/'
    $ageRounded = [math]::Round($AgeMinutes, 1)

    $requiresHuman = 'false'
    $canWrite = Read-FrontMatterValue -Lines ([System.IO.File]::ReadAllLines($WorkingPath, [System.Text.Encoding]::UTF8)) -Key 'can_write'
    if ($canWrite -eq 'true') {
        $requiresHuman = 'true'
    }

    $bodyLockLine = if ([string]::IsNullOrWhiteSpace($LockPath)) { '- Lock file: not found' } else { "- Lock file: $lockForMd" }

    $content = @"
---
id: $notificationId
task: $Task
from: Orchestrator
to: CodeX
type: blocker
status: stale_claim
priority: $Priority
reply_to: $MessageId
requires_human: $requiresHuman
created_at: $createdAt
can_write: false
context_files:
  - $workingForMd
allowed_files: []
forbidden_files:
  - .env
  - migrations/**
attempt: 0
max_attempts: 1
timeout_minutes: 5
---

# Stale Claim Detected

## Summary
- Agent: $Agent
- Message: $MessageId
- Task: $Task
- Status: $Status
- Age: $ageRounded minutes
- Reason: $Reason
- Working file: $workingForMd
$bodyLockLine

## Required Action
- Inspect the working file and any related diff before recovery.
- Do not automatically release write-task locks.
- CodeX or Human should decide whether to move the task back to `ready`, mark it `needs_revision`, `cancelled`, `done`, or `needs_manual_recovery`.

## Scope
- This notification was generated by `check-stale-claims.ps1`.
- The original working task and lock file were not modified.
"@

    if (-not $DryRun) {
        [System.IO.File]::WriteAllText($notificationPath, $content, [System.Text.UTF8Encoding]::new($false))
    }

    return $notificationPath
}

if ($StaleMinutes -lt 1) {
    throw 'StaleMinutes must be at least 1.'
}

$stateDir = Join-Path $WorkgroupRoot 'state'
$eventsPath = Join-Path $stateDir 'events.Orchestrator.jsonl'
$locksDir = Join-Path $stateDir 'locks'
$codeXInbox = Join-Path $WorkgroupRoot 'inbox/CodeX'
New-Item -ItemType Directory -Force -Path $stateDir, $codeXInbox | Out-Null

$now = Get-Date
$results = New-Object System.Collections.ArrayList
Write-OrchestratorEvent -Type 'stale_scan_started' -Status "stale_minutes=$StaleMinutes dry_run=$DryRun"

foreach ($agent in $Agents) {
    $workingDir = Join-Path $WorkgroupRoot "working/$agent"
    if (-not (Test-Path -LiteralPath $workingDir -PathType Container)) {
        continue
    }

    $messages = Get-ChildItem -LiteralPath $workingDir -Filter '*.md' -File -ErrorAction SilentlyContinue | Sort-Object Name
    foreach ($message in $messages) {
        $lines = [System.IO.File]::ReadAllLines($message.FullName, [System.Text.Encoding]::UTF8)
        $messageId = Read-FrontMatterValue -Lines $lines -Key 'id'
        $task = Read-FrontMatterValue -Lines $lines -Key 'task'
        $status = Read-FrontMatterValue -Lines $lines -Key 'status'
        $priority = Read-FrontMatterValue -Lines $lines -Key 'priority'
        $claimedAt = Read-FrontMatterValue -Lines $lines -Key 'claimed_at'
        $lockId = Read-FrontMatterValue -Lines $lines -Key 'lock_id'

        if ($status -notin $Statuses) {
            continue
        }

        $ageMinutes = 0.0
        $reason = 'claimed_at'
        if (-not [string]::IsNullOrWhiteSpace($claimedAt)) {
            try {
                $claimedDate = [DateTimeOffset]::Parse($claimedAt).LocalDateTime
                $ageMinutes = ($now - $claimedDate).TotalMinutes
            } catch {
                $ageMinutes = ($now - $message.LastWriteTime).TotalMinutes
                $reason = "invalid_claimed_at:$claimedAt"
            }
        } else {
            $ageMinutes = ($now - $message.LastWriteTime).TotalMinutes
            $reason = 'missing_claimed_at_using_last_write_time'
        }

        if ($ageMinutes -lt $StaleMinutes) {
            continue
        }

        $lockPath = ''
        if (-not [string]::IsNullOrWhiteSpace($lockId)) {
            $candidateLock = Join-Path $locksDir "$lockId.lock"
            if (Test-Path -LiteralPath $candidateLock -PathType Leaf) {
                $lockPath = $candidateLock
            }
        } elseif (-not [string]::IsNullOrWhiteSpace($messageId)) {
            $safeMessageId = ConvertTo-SafeFileName $messageId
            $candidateLock = Join-Path $locksDir "$safeMessageId.lock"
            if (Test-Path -LiteralPath $candidateLock -PathType Leaf) {
                $lockPath = $candidateLock
            }
        }

        $duplicate = Test-ExistingNotification -MessageId $messageId
        if ($duplicate) {
            [void] $results.Add([pscustomobject]@{
                agent = $agent
                message_id = $messageId
                task = $task
                status = $status
                age_minutes = [math]::Round($ageMinutes, 1)
                action = 'skip_duplicate'
                path = $message.FullName
            })
            continue
        }

        $notificationPath = New-StaleNotification -Agent $agent -MessageId $messageId -Task $task -Priority $priority -WorkingPath $message.FullName -LockPath $lockPath -AgeMinutes $ageMinutes -Reason $reason -Status $status
        Write-OrchestratorEvent -Type 'stale_claim_detected' -Agent $agent -MessageId $messageId -Path $message.FullName -Status "notification=$notificationPath"
        [void] $results.Add([pscustomobject]@{
            agent = $agent
            message_id = $messageId
            task = $task
            status = $status
            age_minutes = [math]::Round($ageMinutes, 1)
            action = $(if ($DryRun) { 'would_notify' } else { 'notified' })
            path = $message.FullName
            notification_path = $notificationPath
        })
    }
}

if (Test-Path -LiteralPath $locksDir -PathType Container) {
    $locks = Get-ChildItem -LiteralPath $locksDir -Filter '*.lock' -File -ErrorAction SilentlyContinue | Sort-Object Name
    foreach ($lock in $locks) {
        try {
            $payload = [System.IO.File]::ReadAllText($lock.FullName, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        } catch {
            continue
        }

        $agent = [string]$payload.agent
        if ($agent -notin $Agents) {
            continue
        }

        $sourcePath = [string]$payload.source_path
        if ([string]::IsNullOrWhiteSpace($sourcePath) -or -not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            continue
        }

        $sourceFullPath = (Resolve-Path -LiteralPath $sourcePath).ProviderPath
        $agentInbox = Join-Path $WorkgroupRoot "inbox/$agent"
        if (-not (Test-Path -LiteralPath $agentInbox -PathType Container)) {
            continue
        }
        $agentInboxFullPath = (Resolve-Path -LiteralPath $agentInbox).ProviderPath
        if (-not $sourceFullPath.StartsWith($agentInboxFullPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            continue
        }

        $lines = [System.IO.File]::ReadAllLines($sourceFullPath, [System.Text.Encoding]::UTF8)
        $messageId = Read-FrontMatterValue -Lines $lines -Key 'id'
        $task = Read-FrontMatterValue -Lines $lines -Key 'task'
        $status = Read-FrontMatterValue -Lines $lines -Key 'status'
        $priority = Read-FrontMatterValue -Lines $lines -Key 'priority'
        if ($status -ne 'ready') {
            continue
        }

        $createdAt = [string]$payload.created_at
        $ageMinutes = 0.0
        $reason = 'orphan_lock_created_at'
        if (-not [string]::IsNullOrWhiteSpace($createdAt)) {
            try {
                $createdDate = [DateTimeOffset]::Parse($createdAt).LocalDateTime
                $ageMinutes = ($now - $createdDate).TotalMinutes
            } catch {
                $ageMinutes = ($now - $lock.LastWriteTime).TotalMinutes
                $reason = "invalid_lock_created_at:$createdAt"
            }
        } else {
            $ageMinutes = ($now - $lock.LastWriteTime).TotalMinutes
            $reason = 'missing_lock_created_at_using_lock_last_write_time'
        }

        if ($ageMinutes -lt $StaleMinutes) {
            continue
        }

        $duplicate = Test-ExistingNotification -MessageId $messageId
        if ($duplicate) {
            [void] $results.Add([pscustomobject]@{
                agent = $agent
                message_id = $messageId
                task = $task
                status = $status
                age_minutes = [math]::Round($ageMinutes, 1)
                action = 'skip_duplicate_orphan_lock'
                path = $sourceFullPath
                lock_path = $lock.FullName
            })
            continue
        }

        $notificationPath = New-StaleNotification -Agent $agent -MessageId $messageId -Task $task -Priority $priority -WorkingPath $sourceFullPath -LockPath $lock.FullName -AgeMinutes $ageMinutes -Reason $reason -Status 'ready_with_orphan_lock'
        Write-OrchestratorEvent -Type 'orphan_lock_detected' -Agent $agent -MessageId $messageId -Path $sourceFullPath -Status "lock=$($lock.FullName) notification=$notificationPath"
        [void] $results.Add([pscustomobject]@{
            agent = $agent
            message_id = $messageId
            task = $task
            status = 'ready_with_orphan_lock'
            age_minutes = [math]::Round($ageMinutes, 1)
            action = $(if ($DryRun) { 'would_notify_orphan_lock' } else { 'notified_orphan_lock' })
            path = $sourceFullPath
            lock_path = $lock.FullName
            notification_path = $notificationPath
        })
    }
}

Write-OrchestratorEvent -Type 'stale_scan_finished' -Status "matches=$($results.Count)"

if ($Json) {
    if ($results.Count -eq 0) {
        Write-Output '[]'
    } else {
        @($results) | ConvertTo-Json -Depth 6
    }
} else {
    @($results) | Format-Table -AutoSize
}
