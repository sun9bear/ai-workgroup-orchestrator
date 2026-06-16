param(
    [string] $Agent = 'Fake',
    [string] $WorkgroupRoot = 'docs/ai-workgroup',
    [string] $MessageId = '',
    [string] $RunnerId = "$env:COMPUTERNAME-$PID",
    [int] $HoldMilliseconds = 0,
    [switch] $Json
)

$ErrorActionPreference = 'Stop'

function New-IsoTimestamp {
    return (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
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

function Set-FrontMatterValue {
    param(
        [string] $FilePath,
        [string] $Key,
        [string] $Value
    )

    $lines = [System.IO.File]::ReadAllLines($FilePath, [System.Text.Encoding]::UTF8)
    $endIndex = -1
    for ($i = 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i].Trim() -eq '---') {
            $endIndex = $i
            break
        }
    }
    if ($endIndex -lt 0) {
        throw "Missing closing front matter delimiter in $FilePath"
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
        $newLines = New-Object System.Collections.Generic.List[string]
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($i -eq $endIndex) {
                $newLines.Add("${Key}: $Value")
            }
            $newLines.Add($lines[$i])
        }
        $lines = $newLines.ToArray()
    }

    [System.IO.File]::WriteAllLines($FilePath, [string[]]$lines, [System.Text.UTF8Encoding]::new($false))
}

function Write-AgentEvent {
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

function Write-Result {
    param([hashtable] $Result)
    if ($Json) {
        [pscustomobject]$Result | ConvertTo-Json -Compress
    } else {
        if ($Result.claimed) {
            Write-Output "CLAIMED $($Result.message_id) -> $($Result.working_path)"
        } else {
            Write-Output "NOT_CLAIMED $($Result.reason) $($Result.message_id)"
        }
    }
}

$inbox = Join-Path $WorkgroupRoot "inbox/$Agent"
$working = Join-Path $WorkgroupRoot "working/$Agent"
$locks = Join-Path $WorkgroupRoot 'state/locks'
$eventsPath = Join-Path $WorkgroupRoot "state/events.$Agent.jsonl"

New-Item -ItemType Directory -Force -Path $working, $locks, (Split-Path $eventsPath -Parent) | Out-Null

$candidates = Get-ChildItem -LiteralPath $inbox -Filter '*.md' -File -ErrorAction SilentlyContinue | Sort-Object Name
if (-not [string]::IsNullOrWhiteSpace($MessageId)) {
    $matched = New-Object System.Collections.ArrayList
    foreach ($candidate in $candidates) {
        $candidateLines = [System.IO.File]::ReadAllLines($candidate.FullName, [System.Text.Encoding]::UTF8)
        $candidateId = Read-FrontMatterValue -Lines $candidateLines -Key 'id'
        if ($candidateId -eq $MessageId) {
            [void] $matched.Add($candidate)
        }
    }
    $candidates = @($matched)
}

$message = @($candidates) | Select-Object -First 1
if ($null -eq $message) {
    Write-Result @{
        claimed = $false
        reason = 'no_message'
        message_id = $MessageId
    }
    exit 0
}

$lines = [System.IO.File]::ReadAllLines($message.FullName, [System.Text.Encoding]::UTF8)
$id = Read-FrontMatterValue -Lines $lines -Key 'id'
$status = Read-FrontMatterValue -Lines $lines -Key 'status'
if ([string]::IsNullOrWhiteSpace($id)) {
    throw "Message id is missing in $($message.FullName)"
}
if ($status -ne 'ready') {
    Write-Result @{
        claimed = $false
        reason = "status_$status"
        message_id = $id
    }
    exit 3
}

$lockId = ConvertTo-SafeFileName $id
$lockPath = Join-Path $locks "$lockId.lock"
$lockPayload = [ordered]@{
    message_id = $id
    agent = $Agent
    runner_id = $RunnerId
    created_at = (New-IsoTimestamp)
    source_path = $message.FullName
} | ConvertTo-Json -Compress

try {
    New-Item -Path $lockPath -ItemType File -Value $lockPayload -ErrorAction Stop | Out-Null
} catch {
    if (Test-Path -LiteralPath $lockPath) {
        Write-Result @{
            claimed = $false
            reason = 'locked'
            message_id = $id
            lock_path = $lockPath
        }
        exit 2
    }

    Write-AgentEvent -Type 'claim_failed' -MessageId $id -Path $message.FullName -Status "lock_create_failed: $($_.Exception.Message)"
    Write-Result @{
        claimed = $false
        reason = 'lock_create_failed'
        message_id = $id
        error = $_.Exception.Message
    }
    exit 1
}

try {
    if ($HoldMilliseconds -gt 0) {
        Start-Sleep -Milliseconds $HoldMilliseconds
    }

    $workingPath = Join-Path $working $message.Name
    Move-Item -LiteralPath $message.FullName -Destination $workingPath
    $claimedAt = New-IsoTimestamp
    Set-FrontMatterValue -FilePath $workingPath -Key 'status' -Value 'claimed'
    Set-FrontMatterValue -FilePath $workingPath -Key 'claimed_by' -Value $RunnerId
    Set-FrontMatterValue -FilePath $workingPath -Key 'claimed_at' -Value $claimedAt
    Set-FrontMatterValue -FilePath $workingPath -Key 'lock_id' -Value $lockId
    Write-AgentEvent -Type 'task_claimed' -MessageId $id -Path $workingPath

    Write-Result @{
        claimed = $true
        reason = 'claimed'
        message_id = $id
        lock_path = $lockPath
        working_path = $workingPath
    }
    exit 0
} catch {
    if (Test-Path -LiteralPath $lockPath) {
        Remove-Item -LiteralPath $lockPath -Force
    }
    Write-AgentEvent -Type 'claim_failed' -MessageId $id -Path $message.FullName -Status $_.Exception.Message
    throw
}
