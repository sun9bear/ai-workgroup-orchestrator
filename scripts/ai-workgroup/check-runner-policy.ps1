param(
    [string] $Agent,
    [string] $MessagePath = '',
    [string] $WorkgroupRoot = 'docs/ai-workgroup',
    [string] $PolicyPath = '',
    [switch] $Record,
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

function Write-Result {
    param([hashtable] $Result)

    $object = [pscustomobject]$Result
    if ($Json) {
        $object | ConvertTo-Json -Depth 6 -Compress
    } else {
        $object
    }
}

if ([string]::IsNullOrWhiteSpace($Agent)) {
    throw 'Agent is required.'
}

if ([string]::IsNullOrWhiteSpace($PolicyPath)) {
    $PolicyPath = Join-Path $WorkgroupRoot 'shared/runner-policy.json'
}

if (-not (Test-Path -LiteralPath $PolicyPath -PathType Leaf)) {
    throw "Runner policy was not found: $PolicyPath"
}

$policy = Get-Content -Raw -Encoding UTF8 -LiteralPath $PolicyPath | ConvertFrom-Json
$agentPolicy = $policy.agents.PSObject.Properties[$Agent].Value

$today = (Get-Date).ToString('yyyy-MM-dd')
$usagePath = Join-Path $WorkgroupRoot "state/runner-usage.$today.jsonl"
New-Item -ItemType Directory -Force -Path (Split-Path $usagePath -Parent) | Out-Null

$messageId = ''
$canWrite = 'false'
$requiresHuman = 'false'
if (-not [string]::IsNullOrWhiteSpace($MessagePath) -and (Test-Path -LiteralPath $MessagePath -PathType Leaf)) {
    $lines = [System.IO.File]::ReadAllLines($MessagePath, [System.Text.Encoding]::UTF8)
    $messageId = Read-FrontMatterValue -Lines $lines -Key 'id'
    $canWrite = Read-FrontMatterValue -Lines $lines -Key 'can_write'
    $requiresHuman = Read-FrontMatterValue -Lines $lines -Key 'requires_human'
}

$reasons = New-Object System.Collections.ArrayList

if ($policy.kill_switch -eq $true) {
    [void] $reasons.Add('kill_switch_enabled')
}

if ($null -eq $agentPolicy) {
    [void] $reasons.Add('agent_not_configured')
    $agentPolicy = [pscustomobject]@{
        enabled = $false
        daily_limit = 0
        timeout_seconds = 0
        max_budget_usd = 0
        allow_write = $false
    }
}

if ($agentPolicy.enabled -ne $true) {
    [void] $reasons.Add('agent_disabled')
}

if ($canWrite -eq 'true' -and $agentPolicy.allow_write -ne $true) {
    [void] $reasons.Add('write_tasks_disabled_for_agent')
}

if ($requiresHuman -eq 'true') {
    [void] $reasons.Add('requires_human')
}

$dailyLimit = [int]$agentPolicy.daily_limit
$usedToday = 0
if (Test-Path -LiteralPath $usagePath -PathType Leaf) {
    foreach ($line in [System.IO.File]::ReadLines($usagePath, [System.Text.Encoding]::UTF8)) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        try {
            $entry = $line | ConvertFrom-Json
            if ($entry.agent -eq $Agent -and $entry.date -eq $today) {
                $usedToday++
            }
        } catch {
            continue
        }
    }
}

if ($dailyLimit -lt 1) {
    [void] $reasons.Add('daily_limit_zero')
} elseif ($usedToday -ge $dailyLimit) {
    [void] $reasons.Add("daily_limit_reached:$usedToday/$dailyLimit")
}

$allowed = ($reasons.Count -eq 0)

if ($allowed -and $Record) {
    $usage = [ordered]@{
        at = New-IsoTimestamp
        date = $today
        agent = $Agent
        message_id = $messageId
        message_path = $MessagePath
    }
    Add-ContentWithRetry -Path $usagePath -Value ($usage | ConvertTo-Json -Compress)
    $usedToday++
}

Write-Result @{
    allowed = $allowed
    agent = $Agent
    message_id = $messageId
    reasons = @($reasons)
    used_today = $usedToday
    daily_limit = $dailyLimit
    timeout_seconds = [int]$agentPolicy.timeout_seconds
    max_budget_usd = [decimal]$agentPolicy.max_budget_usd
    allow_write = [bool]$agentPolicy.allow_write
    requires_human = ($requiresHuman -eq 'true')
    policy_path = $PolicyPath
    usage_path = $usagePath
}
