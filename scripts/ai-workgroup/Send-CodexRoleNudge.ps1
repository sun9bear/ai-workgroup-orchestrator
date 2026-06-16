param(
    [ValidateSet('Nudge', 'Pause', 'Status', 'Sweep')]
    [string] $Action = 'Nudge',
    [ValidateSet('TechLead', 'CodeX', 'Reviewer', 'GitSteward', 'Git-Steward')]
    [string] $Role = 'TechLead',
    [string] $ThreadId = '',
    [string] $Reason = '',
    [string] $Task = '',
    [string] $SourcePath = '',
    [string] $CodexHome = '',
    [int] $CooldownMinutes = 15,
    [int] $ExpireAfterMinutes = 15,
    [switch] $Force,
    [switch] $Json
)

$ErrorActionPreference = 'Stop'
$NudgeStartMarker = '--- AIWG_MECHANISM_NUDGE ---'
$NudgeEndMarker = '--- END_AIWG_MECHANISM_NUDGE ---'

if ($Action -ne 'Status') {
    $disabled = [pscustomobject]@{
        ok = $false
        action = 'disabled'
        requested_action = $Action
        role = $Role
        message = 'Codex automation mutation is disabled by policy. This script no longer writes automation.toml.'
    }
    if ($Json) {
        $disabled | ConvertTo-Json -Depth 4
    } else {
        Write-Output $disabled.message
    }
    exit 0
}

function Get-NowUnixMilliseconds {
    return [int64]([datetimeoffset]::UtcNow.ToUnixTimeMilliseconds())
}

function ConvertTo-TomlBasicString {
    param([string] $Value)
    if ($null -eq $Value) {
        $Value = ''
    }
    $escaped = $Value -replace '\\', '\\'
    $escaped = $escaped -replace '"', '\"'
    $escaped = $escaped -replace "`r`n", '\n'
    $escaped = $escaped -replace "`n", '\n'
    $escaped = $escaped -replace "`r", '\n'
    return '"' + $escaped + '"'
}

function Get-CodexHome {
    param([string] $Provided)
    if (-not [string]::IsNullOrWhiteSpace($Provided)) {
        return $Provided
    }
    if (-not [string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
        return $env:CODEX_HOME
    }
    return (Join-Path $env:USERPROFILE '.codex')
}

function Get-RoleConfig {
    param([string] $RequestedRole, [string] $OverrideThreadId)
    $roles = @{
        TechLead = @{
            automation_id = 'aivideotrans-tech-lead-planner'
            legacy_automation_id = 'aivideotrans-nudge-tech-lead'
            display = 'Tech Lead / Planner'
            inbox = 'CodeX'
            thread_id = '019e88c3-57c2-7e11-8ca6-83b7550fb799'
        }
        CodeX = @{
            automation_id = 'aivideotrans-tech-lead-planner'
            legacy_automation_id = 'aivideotrans-nudge-tech-lead'
            display = 'Tech Lead / Planner'
            inbox = 'CodeX'
            thread_id = '019e88c3-57c2-7e11-8ca6-83b7550fb799'
        }
        Reviewer = @{
            automation_id = 'aivideotrans-reviewer'
            legacy_automation_id = 'aivideotrans-nudge-reviewer'
            display = 'Reviewer'
            inbox = 'Reviewer'
            thread_id = '019e88c4-686e-7e80-8be9-1ddcf570059c'
        }
        GitSteward = @{
            automation_id = 'aivideotrans-git-steward'
            legacy_automation_id = 'aivideotrans-nudge-git-steward'
            display = 'Git Steward'
            inbox = 'Git-Steward'
            thread_id = '019e88c5-08d5-73d0-aebe-44fe49c78101'
        }
        'Git-Steward' = @{
            automation_id = 'aivideotrans-git-steward'
            legacy_automation_id = 'aivideotrans-nudge-git-steward'
            display = 'Git Steward'
            inbox = 'Git-Steward'
            thread_id = '019e88c5-08d5-73d0-aebe-44fe49c78101'
        }
    }
    $config = $roles[$RequestedRole]
    if ([string]::IsNullOrWhiteSpace($config.thread_id) -and [string]::IsNullOrWhiteSpace($OverrideThreadId)) {
        throw "No target thread id configured for role $RequestedRole."
    }
    if (-not [string]::IsNullOrWhiteSpace($OverrideThreadId)) {
        $config.thread_id = $OverrideThreadId
    }
    return $config
}

function Read-AutomationTomlInfo {
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{
            exists = $false
            status = ''
            updated_at = 0
            nudge_updated_at = 0
            target_thread_id = ''
        }
    }
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    $status = [regex]::Match($raw, '(?m)^status\s*=\s*"?([^"\r\n]+)"?').Groups[1].Value
    $updatedRaw = [regex]::Match($raw, '(?m)^updated_at\s*=\s*(\d+)').Groups[1].Value
    $nudgeUpdatedRaw = [regex]::Match($raw, '(?m)^nudge_updated_at\s*=\s*(\d+)').Groups[1].Value
    $thread = [regex]::Match($raw, '(?m)^target_thread_id\s*=\s*"([^"]+)"').Groups[1].Value
    $updated = 0L
    if (-not [string]::IsNullOrWhiteSpace($updatedRaw)) {
        [void][int64]::TryParse($updatedRaw, [ref]$updated)
    }
    $nudgeUpdated = 0L
    if (-not [string]::IsNullOrWhiteSpace($nudgeUpdatedRaw)) {
        [void][int64]::TryParse($nudgeUpdatedRaw, [ref]$nudgeUpdated)
    }
    return [pscustomobject]@{
        exists = $true
        status = $status
        updated_at = $updated
        nudge_updated_at = $nudgeUpdated
        target_thread_id = $thread
    }
}

function ConvertFrom-TomlBasicString {
    param([string] $Value)
    if ($null -eq $Value) {
        return ''
    }
    $builder = [System.Text.StringBuilder]::new()
    for ($i = 0; $i -lt $Value.Length; $i++) {
        $ch = $Value[$i]
        if ($ch -eq '\' -and $i + 1 -lt $Value.Length) {
            $next = $Value[$i + 1]
            switch ($next) {
                'n' { [void]$builder.Append("`n"); $i++; continue }
                'r' { [void]$builder.Append("`r"); $i++; continue }
                't' { [void]$builder.Append("`t"); $i++; continue }
                '"' { [void]$builder.Append('"'); $i++; continue }
                '\' { [void]$builder.Append('\'); $i++; continue }
                default { [void]$builder.Append($next); $i++; continue }
            }
        }
        [void]$builder.Append($ch)
    }
    return $builder.ToString()
}

function Get-TomlBasicStringValue {
    param([string] $Raw, [string] $Key)
    $match = [regex]::Match($Raw, "(?m)^$([regex]::Escape($Key))\s*=\s*`"((?:\\.|[^`"\\])*)`"")
    if (-not $match.Success) {
        return ''
    }
    return ConvertFrom-TomlBasicString -Value $match.Groups[1].Value
}

function Set-TomlBasicStringValue {
    param([string] $Raw, [string] $Key, [string] $Value)
    $line = "$Key = $(ConvertTo-TomlBasicString $Value)"
    if ($Raw -match "(?m)^$([regex]::Escape($Key))\s*=") {
        return [regex]::Replace($Raw, "(?m)^$([regex]::Escape($Key))\s*=.*$", $line)
    }
    return ($Raw.TrimEnd() + "`n" + $line + "`n")
}

function Set-TomlIntegerValue {
    param([string] $Raw, [string] $Key, [int64] $Value)
    $line = "$Key = $Value"
    if ($Raw -match "(?m)^$([regex]::Escape($Key))\s*=") {
        return [regex]::Replace($Raw, "(?m)^$([regex]::Escape($Key))\s*=.*$", $line)
    }
    return ($Raw.TrimEnd() + "`n" + $line + "`n")
}

function Remove-NudgeSection {
    param([string] $Prompt)
    if ([string]::IsNullOrWhiteSpace($Prompt)) {
        return ''
    }
    $pattern = "(?s)\r?\n?\r?\n?$([regex]::Escape($NudgeStartMarker)).*?$([regex]::Escape($NudgeEndMarker))\r?\n?"
    return ([regex]::Replace($Prompt, $pattern, "`n")).TrimEnd()
}

function Pause-LegacyNudgeAutomation {
    param([string] $CodexRoot, $RoleConfig)
    if ([string]::IsNullOrWhiteSpace($RoleConfig.legacy_automation_id)) {
        return
    }
    $legacyPath = Join-Path $CodexRoot "automations/$($RoleConfig.legacy_automation_id)/automation.toml"
    if (Test-Path -LiteralPath $legacyPath -PathType Leaf) {
        [void](Set-AutomationStatus -Path $legacyPath -Status 'PAUSED')
    }
}

function New-NudgePrompt {
    param(
        $RoleConfig,
        [string] $ReasonText,
        [string] $TaskText,
        [string] $SourcePathText
    )

    $taskLine = if ([string]::IsNullOrWhiteSpace($TaskText)) { '未指定；请按你的角色读取当前 inbox/working/done 状态。' } else { $TaskText }
    $pathLine = if ([string]::IsNullOrWhiteSpace($SourcePathText)) { '未指定。' } else { $SourcePathText }
    $reasonLine = if ([string]::IsNullOrWhiteSpace($ReasonText)) { 'watchdog 检测到你的角色可能没有及时消费队列。' } else { $ReasonText }

    return @"
$NudgeStartMarker
你是 AIVideoTrans AI workgroup 的 $($RoleConfig.display)。

这是 mechanism watchdog 发出的「一次性唤醒提示」，不是业务决策，也不是新的实现任务。

请执行：
1. 读取项目 `D:\example\protected-business-repo` 的 `docs\ai-workgroup` 状态。
2. 只处理属于你角色职责的已有 ready/reported/failed/blocker 消息。
3. 不要创建重复任务；如果已有任务正在执行或没有你的待处理项，请简短说明当前等待对象。
4. 不要替 Human 做决策，不要越过 phase envelope，不要碰美国生产环境。
5. 默认用简体中文写报告；代码符号、路径、命令和 front matter 字段保持英文。

触发原因：
$reasonLine

相关任务：
$taskLine

相关路径：
$pathLine
$NudgeEndMarker
"@
}

function Write-NudgeAutomation {
    param(
        [string] $AutomationRoot,
        $RoleConfig,
        [string] $Prompt,
        [string] $Status,
        [int64] $CreatedAt,
        [int64] $UpdatedAt
    )
    New-Item -ItemType Directory -Force -Path $AutomationRoot | Out-Null
    $tomlPath = Join-Path $AutomationRoot 'automation.toml'
    $content = @(
        'version = 1'
        "id = $(ConvertTo-TomlBasicString $RoleConfig.automation_id)"
        'kind = "heartbeat"'
        "name = $(ConvertTo-TomlBasicString ('AIVideoTrans Nudge - ' + $RoleConfig.display))"
        "prompt = $(ConvertTo-TomlBasicString $Prompt)"
        "status = $(ConvertTo-TomlBasicString $Status)"
        'rrule = "FREQ=MINUTELY;INTERVAL=1"'
        "target_thread_id = $(ConvertTo-TomlBasicString $RoleConfig.thread_id)"
        "created_at = $CreatedAt"
        "updated_at = $UpdatedAt"
        ''
    ) -join "`n"
    [System.IO.File]::WriteAllText($tomlPath, $content, [System.Text.UTF8Encoding]::new($false))
    return $tomlPath
}

function Set-AutomationStatus {
    param([string] $Path, [string] $Status)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if ($raw -match '(?m)^status\s*=') {
        $raw = [regex]::Replace($raw, '(?m)^status\s*=.*$', "status = `"$Status`"")
    } else {
        $raw += "`nstatus = `"$Status`"`n"
    }
    $now = Get-NowUnixMilliseconds
    if ($raw -match '(?m)^updated_at\s*=') {
        $raw = [regex]::Replace($raw, '(?m)^updated_at\s*=.*$', "updated_at = $now")
    } else {
        $raw += "updated_at = $now`n"
    }
    [System.IO.File]::WriteAllText($Path, $raw, [System.Text.UTF8Encoding]::new($false))
    return $true
}

function Update-RoleHeartbeatWithNudge {
    param(
        [string] $AutomationRoot,
        $RoleConfig,
        [string] $NudgePrompt,
        [int64] $UpdatedAt
    )
    New-Item -ItemType Directory -Force -Path $AutomationRoot | Out-Null
    $tomlPath = Join-Path $AutomationRoot 'automation.toml'
    if (Test-Path -LiteralPath $tomlPath -PathType Leaf) {
        $raw = Get-Content -LiteralPath $tomlPath -Raw -Encoding UTF8
    } else {
        $raw = @(
            'version = 1'
            "id = $(ConvertTo-TomlBasicString $RoleConfig.automation_id)"
            'kind = "heartbeat"'
            "name = $(ConvertTo-TomlBasicString ('AIVideoTrans ' + $RoleConfig.display))"
            'prompt = ""'
            'status = "ACTIVE"'
            'rrule = "FREQ=MINUTELY;INTERVAL=5"'
            "target_thread_id = $(ConvertTo-TomlBasicString $RoleConfig.thread_id)"
            "created_at = $UpdatedAt"
            "updated_at = $UpdatedAt"
            ''
        ) -join "`n"
    }

    $basePrompt = Get-TomlBasicStringValue -Raw $raw -Key 'prompt'
    $basePrompt = Remove-NudgeSection -Prompt $basePrompt
    $mergedPrompt = ($basePrompt.TrimEnd() + "`n`n" + $NudgePrompt.Trim()).Trim()

    $raw = Set-TomlBasicStringValue -Raw $raw -Key 'id' -Value $RoleConfig.automation_id
    $raw = Set-TomlBasicStringValue -Raw $raw -Key 'kind' -Value 'heartbeat'
    $raw = Set-TomlBasicStringValue -Raw $raw -Key 'name' -Value ('AIVideoTrans ' + $RoleConfig.display)
    $raw = Set-TomlBasicStringValue -Raw $raw -Key 'prompt' -Value $mergedPrompt
    $raw = Set-TomlBasicStringValue -Raw $raw -Key 'status' -Value 'ACTIVE'
    $raw = Set-TomlBasicStringValue -Raw $raw -Key 'rrule' -Value 'FREQ=MINUTELY;INTERVAL=1'
    $raw = Set-TomlBasicStringValue -Raw $raw -Key 'target_thread_id' -Value $RoleConfig.thread_id
    $raw = Set-TomlIntegerValue -Raw $raw -Key 'updated_at' -Value $UpdatedAt
    $raw = Set-TomlIntegerValue -Raw $raw -Key 'nudge_updated_at' -Value $UpdatedAt
    [System.IO.File]::WriteAllText($tomlPath, $raw.TrimEnd() + "`n", [System.Text.UTF8Encoding]::new($false))
    return $tomlPath
}

function Clear-RoleHeartbeatNudge {
    param([string] $AutomationRoot)
    $tomlPath = Join-Path $AutomationRoot 'automation.toml'
    if (-not (Test-Path -LiteralPath $tomlPath -PathType Leaf)) {
        return $false
    }
    $raw = Get-Content -LiteralPath $tomlPath -Raw -Encoding UTF8
    $prompt = Get-TomlBasicStringValue -Raw $raw -Key 'prompt'
    $cleanPrompt = Remove-NudgeSection -Prompt $prompt
    if ($cleanPrompt -eq $prompt) {
        return $false
    }
    $now = Get-NowUnixMilliseconds
    $raw = Set-TomlBasicStringValue -Raw $raw -Key 'prompt' -Value $cleanPrompt
    $raw = Set-TomlBasicStringValue -Raw $raw -Key 'rrule' -Value 'FREQ=MINUTELY;INTERVAL=5'
    $raw = Set-TomlIntegerValue -Raw $raw -Key 'updated_at' -Value $now
    $raw = Set-TomlIntegerValue -Raw $raw -Key 'nudge_updated_at' -Value 0
    [System.IO.File]::WriteAllText($tomlPath, $raw.TrimEnd() + "`n", [System.Text.UTF8Encoding]::new($false))
    return $true
}

function Invoke-RoleAction {
    param([string] $RequestedRole)
    $codexRoot = Get-CodexHome -Provided $CodexHome
    $roleConfig = Get-RoleConfig -RequestedRole $RequestedRole -OverrideThreadId $ThreadId
    $automationRoot = Join-Path $codexRoot "automations/$($roleConfig.automation_id)"
    $tomlPath = Join-Path $automationRoot 'automation.toml'
    $info = Read-AutomationTomlInfo -Path $tomlPath
    $now = Get-NowUnixMilliseconds
    $ageMinutes = if ($info.updated_at -gt 0) { [math]::Floor(($now - $info.updated_at) / 60000) } else { $null }
    $nudgeAgeMinutes = if ($info.nudge_updated_at -gt 0) { [math]::Floor(($now - $info.nudge_updated_at) / 60000) } else { $null }
    Pause-LegacyNudgeAutomation -CodexRoot $codexRoot -RoleConfig $roleConfig

    if ($Action -eq 'Status') {
        return [pscustomobject]@{
            ok = $true
            action = 'status'
            role = $RequestedRole
            automation_id = $roleConfig.automation_id
            path = $tomlPath
            exists = $info.exists
            status = $info.status
            age_minutes = $ageMinutes
            nudge_age_minutes = $nudgeAgeMinutes
            target_thread_id = $info.target_thread_id
        }
    }

    if ($Action -eq 'Pause') {
        $changed = Clear-RoleHeartbeatNudge -AutomationRoot $automationRoot
        return [pscustomobject]@{
            ok = $true
            action = 'nudge_cleared'
            role = $RequestedRole
            automation_id = $roleConfig.automation_id
            path = $tomlPath
            changed = $changed
        }
    }

    if ($Action -eq 'Sweep') {
        if ($nudgeAgeMinutes -ne $null -and $nudgeAgeMinutes -ge $ExpireAfterMinutes) {
            [void](Clear-RoleHeartbeatNudge -AutomationRoot $automationRoot)
            return [pscustomobject]@{
                ok = $true
                action = 'sweep_cleared_nudge'
                role = $RequestedRole
                automation_id = $roleConfig.automation_id
                path = $tomlPath
                nudge_age_minutes = $nudgeAgeMinutes
            }
        }
        return [pscustomobject]@{
            ok = $true
            action = 'sweep_noop'
            role = $RequestedRole
            automation_id = $roleConfig.automation_id
            path = $tomlPath
            age_minutes = $ageMinutes
            nudge_age_minutes = $nudgeAgeMinutes
            status = $info.status
        }
    }

    if (-not $Force -and $nudgeAgeMinutes -ne $null -and $nudgeAgeMinutes -lt $CooldownMinutes) {
        return [pscustomobject]@{
            ok = $true
            action = 'cooldown_skip'
            role = $RequestedRole
            automation_id = $roleConfig.automation_id
            path = $tomlPath
            nudge_age_minutes = $nudgeAgeMinutes
            cooldown_minutes = $CooldownMinutes
        }
    }

    $prompt = New-NudgePrompt -RoleConfig $roleConfig -ReasonText $Reason -TaskText $Task -SourcePathText $SourcePath
    $path = Update-RoleHeartbeatWithNudge -AutomationRoot $automationRoot -RoleConfig $roleConfig -NudgePrompt $prompt -UpdatedAt $now
    return [pscustomobject]@{
        ok = $true
        action = 'role_heartbeat_nudge_written'
        role = $RequestedRole
        automation_id = $roleConfig.automation_id
        path = $path
        target_thread_id = $roleConfig.thread_id
        cooldown_minutes = $CooldownMinutes
        expire_after_minutes = $ExpireAfterMinutes
    }
}

try {
    if ($Action -eq 'Sweep') {
        $roles = @('TechLead', 'Reviewer', 'GitSteward')
        $results = foreach ($item in $roles) {
            Invoke-RoleAction -RequestedRole $item
        }
        if ($Json) {
            $results | ConvertTo-Json -Depth 5
        } else {
            $results
        }
        exit 0
    }

    $result = Invoke-RoleAction -RequestedRole $Role
    if ($Json) {
        $result | ConvertTo-Json -Depth 5
    } else {
        $result
    }
    exit 0
} catch {
    $errorResult = [pscustomobject]@{
        ok = $false
        action = $Action
        role = $Role
        error = $_.Exception.Message
    }
    if ($Json) {
        $errorResult | ConvertTo-Json -Depth 5
    } else {
        Write-Error $_.Exception.Message
    }
    exit 1
}
