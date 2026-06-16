param(
    [Parameter(Mandatory = $true)]
    [string] $ProjectRoot,
    [string] $WorkgroupRelativePath = 'docs/ai-workgroup',
    [string[]] $PlanFiles = @('docs/plans/2026-06-01-anonymous-preview-funnel-ux-plan.md'),
    [string] $PhaseEnvelopePath = '',
    [switch] $AllowWrite,
    [decimal] $MaxBudgetUsd = 3.00,
    [int] $TimeoutSeconds = 1200,
    [switch] $Json
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

function Write-CoordinatorResult {
    param($Result)
    if ($Json) {
        $Result | ConvertTo-Json -Depth 10
    } else {
        Write-Output "Coordinator action: $($Result.action)"
        if ($Result.message_id) {
            Write-Output "Message: $($Result.message_id)"
        }
        if ($Result.reason) {
            Write-Output "Reason: $($Result.reason)"
        }
        if ($Result.path) {
            Write-Output "Path: $($Result.path)"
        }
    }
}

function Get-RelativeAiwgPath {
    param(
        [Parameter(Mandatory = $true)][string] $ProjectRoot,
        [Parameter(Mandatory = $true)][string] $Path
    )
    $full = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetFullPath($ProjectRoot)
    if (-not $root.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
        $root += [System.IO.Path]::DirectorySeparatorChar
    }
    if ($full.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        return (($full.Substring($root.Length)) -replace '\\', '/')
    }
    return (($Path -replace '\\', '/'))
}

function Resolve-PhaseEnvelopePath {
    param(
        [Parameter(Mandatory = $true)][string] $ProjectRoot,
        [Parameter(Mandatory = $true)][string] $WorkgroupRoot,
        [string] $ExplicitPath
    )

    $candidates = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
        if ([System.IO.Path]::IsPathRooted($ExplicitPath)) {
            $candidates.Add($ExplicitPath)
        } else {
            $candidates.Add((Join-Path $ProjectRoot $ExplicitPath))
        }
    }
    $candidates.Add((Join-Path $WorkgroupRoot 'shared/phase-envelope.current.json'))

    try {
        $orchestratorRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).ProviderPath
        $projectName = Split-Path -Leaf $ProjectRoot
        if (-not [string]::IsNullOrWhiteSpace($projectName)) {
            $candidates.Add((Join-Path $orchestratorRoot "config/phase-envelopes/$projectName.current.json"))
        }
    } catch {
        # Optional fallback only; project-local envelope is the primary source.
    }

    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).ProviderPath
        }
    }
    return ''
}

function Read-PhaseEnvelope {
    param([string] $Path)

    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
        $data = $raw | ConvertFrom-Json
        return [pscustomobject]@{
            path = $Path
            data = $data
            raw = $raw
            error = ''
        }
    } catch {
        return [pscustomobject]@{
            path = $Path
            data = $null
            raw = ''
            error = $_.Exception.Message
        }
    }
}

function Get-PhaseEnvelopeContextFiles {
    param(
        [Parameter(Mandatory = $true)][string] $ProjectRoot,
        $Envelope
    )

    $items = New-Object System.Collections.Generic.List[string]
    if ($null -eq $Envelope -or [string]::IsNullOrWhiteSpace([string]$Envelope.path)) {
        return @()
    }
    $items.Add((Get-RelativeAiwgPath -ProjectRoot $ProjectRoot -Path $Envelope.path))
    if ($null -ne $Envelope.data -and $null -ne $Envelope.data.source_plan_files) {
        foreach ($plan in @($Envelope.data.source_plan_files)) {
            if (-not [string]::IsNullOrWhiteSpace([string]$plan)) {
                $items.Add(([string]$plan -replace '\\', '/'))
            }
        }
    }
    return @($items | Select-Object -Unique)
}

function Format-PhaseEnvelopeForPrompt {
    param($Envelope)

    if ($null -eq $Envelope) {
        return @"
## Approved Phase Envelope

No `phase-envelope.current.json` was found. CodeX must use the plan files and recent Human reports as the source of authority, and return to Human for any ambiguous boundary.
"@
    }

    if (-not [string]::IsNullOrWhiteSpace([string]$Envelope.error)) {
        return @"
## Approved Phase Envelope

The phase envelope file could not be parsed:

- file: `$($Envelope.path)
- error: `$($Envelope.error)

CodeX must not auto-dispatch implementation tasks until this is clarified.
"@
    }

    $json = $Envelope.data | ConvertTo-Json -Depth 20
    return @"
## Approved Phase Envelope

CodeX must use this approved phase envelope as the current delegation boundary:

- file: `$($Envelope.path)

````json
$json
````

Interpretation rules:

- If `status` is `approved` and the next step stays inside `approved_scope` plus `allowed_files`, CodeX may create a narrow Claude-Code implementation task without asking Human again.
- If the next step matches any `human_required_if` item or touches `forbidden_files`, CodeX must stop and write a Human Gate report.
- Prefer the `preferred_task_size` in `autonomy_policy`; do not split one coherent stage into many micro tasks unless the previous attempt failed and the fix must be isolated.
- CodeX may decide technical direction inside this envelope, but must not edit business code directly.
"@
}

function Get-WorkflowMessages {
    param(
        [Parameter(Mandatory = $true)][string] $WorkgroupRoot
    )

    $messages = New-Object System.Collections.ArrayList
    foreach ($area in @('inbox', 'working')) {
        $base = Join-Path $WorkgroupRoot $area
        if (-not (Test-Path -LiteralPath $base -PathType Container)) {
            continue
        }

        $files = Get-ChildItem -LiteralPath $base -Filter '*.md' -File -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName
        foreach ($file in @($files)) {
            try {
                $front = Read-AiwgFrontMatter -FilePath $file.FullName
                $relFromArea = Resolve-Path -LiteralPath $file.FullName -Relative
                $parts = $file.FullName.Substring($base.Length).TrimStart('\', '/') -split '[\\/]'
                $agent = if ($parts.Count -gt 1) { $parts[0] } else { [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'to') }
                [void] $messages.Add([pscustomobject]@{
                    id = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'id')
                    task = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'task')
                    from = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'from')
                    to = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'to')
                    type = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'type')
                    status = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'status')
                    area = $area
                    agent = $agent
                    path = $file.FullName
                    relative = $relFromArea
                    can_write = ConvertTo-AiwgBool (Get-AiwgFrontMatterValue -FrontMatter $front -Key 'can_write' -Default 'false')
                    requires_human = ConvertTo-AiwgBool (Get-AiwgFrontMatterValue -FrontMatter $front -Key 'requires_human' -Default 'false')
                    mtime = $file.LastWriteTimeUtc.ToString('O')
                })
            } catch {
                [void] $messages.Add([pscustomobject]@{
                    id = ''
                    task = $file.BaseName
                    from = ''
                    to = ''
                    type = 'parse_error'
                    status = 'parse_error'
                    area = $area
                    agent = ''
                    path = $file.FullName
                    relative = $file.FullName
                    can_write = $false
                    requires_human = $true
                    mtime = $file.LastWriteTimeUtc.ToString('O')
                    error = $_.Exception.Message
                })
            }
        }
    }
    return @($messages)
}

function Get-FrontMatterUtc {
    param(
        [Parameter(Mandatory = $true)] $FrontMatter,
        [Parameter(Mandatory = $true)] [datetime] $FallbackUtc
    )

    $createdAt = [string](Get-AiwgFrontMatterValue -FrontMatter $FrontMatter -Key 'created_at')
    if (-not [string]::IsNullOrWhiteSpace($createdAt)) {
        try {
            return ([datetimeoffset]::Parse($createdAt)).UtcDateTime
        } catch {
            return $FallbackUtc
        }
    }
    return $FallbackUtc
}

function Get-ApfPhaseRank {
    param([string] $PhaseKey)
    if ($PhaseKey -match '^APF(\d+)([a-z]?)(?:-(\d+))?$') {
        $major = [int]$Matches[1]
        $letter = [string]$Matches[2]
        $sub = if ([string]::IsNullOrWhiteSpace($Matches[3])) { 0 } else { [int]$Matches[3] }
        $letterRank = 0
        if (-not [string]::IsNullOrWhiteSpace($letter)) {
            $letterRank = ([int][char]$letter.ToLowerInvariant()[0]) - ([int][char]'a') + 1
        }
        return ($major * 10000) + ($letterRank * 100) + $sub
    }
    return -1
}

function Get-RecentContextFiles {
    param(
        [Parameter(Mandatory = $true)][string] $ProjectRoot,
        [Parameter(Mandatory = $true)][string] $WorkgroupRoot,
        [string[]] $PlanFiles
    )

    $items = New-Object System.Collections.Generic.List[string]
    foreach ($plan in @($PlanFiles)) {
        if ([System.IO.Path]::IsPathRooted($plan)) {
            $items.Add((Get-RelativeAiwgPath -ProjectRoot $ProjectRoot -Path $plan))
        } else {
            $items.Add(($plan -replace '\\', '/'))
        }
    }

    $recentDirs = @(
        'inbox/Human',
        'done/Human',
        'inbox/CodeX',
        'done/CodeX',
        'done/Claude-Code'
    )
    foreach ($dir in $recentDirs) {
        $fullDir = Join-Path $WorkgroupRoot $dir
        if (-not (Test-Path -LiteralPath $fullDir -PathType Container)) {
            continue
        }
        $recent = Get-ChildItem -LiteralPath $fullDir -Filter '*.md' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 8
        $added = 0
        foreach ($file in @($recent)) {
            if ($dir -in @('inbox/Human', 'done/Human')) {
                try {
                    $front = Read-AiwgFrontMatter -FilePath $file.FullName
                    if ([string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'from') -ne 'CodeX') {
                        continue
                    }
                } catch {
                    continue
                }
            }
            $items.Add((Get-RelativeAiwgPath -ProjectRoot $ProjectRoot -Path $file.FullName))
            $added += 1
            if ($added -ge 4) {
                break
            }
        }
    }
    return @($items | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
}

function New-CoordinatorFingerprint {
    param(
        [Parameter(Mandatory = $true)][string] $ProjectRoot,
        [Parameter(Mandatory = $true)][string] $WorkgroupRoot,
        [string[]] $PlanFiles,
        [string] $PhaseEnvelopePath = ''
    )

    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($plan in @($PlanFiles)) {
        $path = if ([System.IO.Path]::IsPathRooted($plan)) { $plan } else { Join-Path $ProjectRoot $plan }
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $item = Get-Item -LiteralPath $path
            $parts.Add("plan|$($item.FullName)|$($item.Length)|$($item.LastWriteTimeUtc.ToString('O'))")
        } else {
            $parts.Add("plan_missing|$plan")
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($PhaseEnvelopePath) -and (Test-Path -LiteralPath $PhaseEnvelopePath -PathType Leaf)) {
        $envelopeItem = Get-Item -LiteralPath $PhaseEnvelopePath
        $parts.Add("phase_envelope|$($envelopeItem.FullName)|$($envelopeItem.Length)|$($envelopeItem.LastWriteTimeUtc.ToString('O'))")
    } else {
        $parts.Add("phase_envelope_missing|$PhaseEnvelopePath")
    }

    $recentDirs = @('inbox/Human', 'done/Human', 'inbox/CodeX', 'done/CodeX', 'done/Claude-Code')
    foreach ($dir in $recentDirs) {
        $fullDir = Join-Path $WorkgroupRoot $dir
        if (-not (Test-Path -LiteralPath $fullDir -PathType Container)) {
            continue
        }
        $recent = Get-ChildItem -LiteralPath $fullDir -Filter '*.md' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 8
        foreach ($file in @($recent)) {
            $status = ''
            $task = ''
            try {
                $front = Read-AiwgFrontMatter -FilePath $file.FullName
                $status = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'status')
                $task = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'task')
            } catch {
                $status = 'parse_error'
                $task = $file.BaseName
            }
            $rel = Get-RelativeAiwgPath -ProjectRoot $ProjectRoot -Path $file.FullName
            $parts.Add("msg|$rel|$status|$task|$($file.Length)|$($file.LastWriteTimeUtc.ToString('O'))")
        }
    }

    $payload = $parts -join "`n"
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
    $hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
    return (($hash | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Read-CoordinatorCursor {
    param([Parameter(Mandatory = $true)][string] $Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{}
    }
    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return [pscustomobject]@{ read_error = $_.Exception.Message }
    }
}

function Complete-AnsweredCodeXMessages {
    param(
        [Parameter(Mandatory = $true)][string] $WorkgroupRoot
    )

    $replyIds = @{}
    $coveredPhaseKeys = New-Object System.Collections.ArrayList
    $maxCoveredPhaseRank = -1
    $humanInbox = Join-Path $WorkgroupRoot 'inbox/Human'
    $humanDone = Join-Path $WorkgroupRoot 'done/Human'
    foreach ($humanDir in @($humanInbox, $humanDone)) {
        if (Test-Path -LiteralPath $humanDir -PathType Container) {
            $reports = Get-ChildItem -LiteralPath $humanDir -Filter '*.md' -File -ErrorAction SilentlyContinue
            foreach ($report in @($reports)) {
            try {
                $front = Read-AiwgFrontMatter -FilePath $report.FullName
                if ([string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'from') -eq 'CodeX' -and
                    [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'to') -eq 'Human' -and
                    [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'type') -eq 'report') {
                    $replyTo = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'reply_to')
                    if (-not [string]::IsNullOrWhiteSpace($replyTo)) {
                        $replyIds[$replyTo] = $true
                    }
                    $reportTask = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'task')
                    if ($reportTask -match '^(APF\d+[a-z]?(?:-\d+)?)') {
                        $phaseKey = $Matches[1]
                        $phaseRank = Get-ApfPhaseRank -PhaseKey $phaseKey
                        if ($phaseRank -gt $maxCoveredPhaseRank) {
                            $maxCoveredPhaseRank = $phaseRank
                        }
                        $reportCreatedUtc = Get-FrontMatterUtc -FrontMatter $front -FallbackUtc $report.LastWriteTimeUtc
                        [void] $coveredPhaseKeys.Add([pscustomobject]@{
                            key = $phaseKey
                            rank = $phaseRank
                            created_at_utc = $reportCreatedUtc
                        })
                    }
                }
            } catch {
                continue
            }
            }
        }
    }

    if ($replyIds.Count -eq 0 -and $coveredPhaseKeys.Count -eq 0) {
        return @()
    }

    $closed = New-Object System.Collections.ArrayList
    $codexInbox = Join-Path $WorkgroupRoot 'inbox/CodeX'
    if (-not (Test-Path -LiteralPath $codexInbox -PathType Container)) {
        return @()
    }
    $doneDir = Join-Path $WorkgroupRoot 'done/CodeX'
    New-Item -ItemType Directory -Force -Path $doneDir | Out-Null

    $messages = Get-ChildItem -LiteralPath $codexInbox -Filter '*.md' -File -ErrorAction SilentlyContinue
    foreach ($message in @($messages)) {
        try {
            $front = Read-AiwgFrontMatter -FilePath $message.FullName
            $id = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'id')
            $status = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'status')
            $replyTo = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'reply_to')
            $type = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'type')
            $task = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'task')
            $coveredByPhase = $false
            $supersededByLaterPhase = $false
            if ($type -eq 'report' -and $task -match '^(APF\d+[a-z]?(?:-\d+)?)') {
                $messagePhaseKey = $Matches[1]
                $messagePhaseRank = Get-ApfPhaseRank -PhaseKey $messagePhaseKey
                if ($messagePhaseRank -ge 0 -and $maxCoveredPhaseRank -gt $messagePhaseRank) {
                    $supersededByLaterPhase = $true
                }
                $messageCreatedUtc = Get-FrontMatterUtc -FrontMatter $front -FallbackUtc $message.LastWriteTimeUtc
                foreach ($coverage in @($coveredPhaseKeys)) {
                    if ($coverage.key -eq $messagePhaseKey -and $coverage.created_at_utc -gt $messageCreatedUtc) {
                        $coveredByPhase = $true
                        break
                    }
                }
            }
            if ($status -in @('ready', 'reported') -and ($replyIds.ContainsKey($id) -or $replyIds.ContainsKey($replyTo) -or $coveredByPhase -or $supersededByLaterPhase)) {
                Set-AiwgFrontMatterValue -FilePath $message.FullName -Key 'status' -Value 'done'
                $destination = Join-Path $doneDir $message.Name
                Move-Item -LiteralPath $message.FullName -Destination $destination -Force
                [void] $closed.Add([pscustomobject]@{
                    id = $id
                    path = $destination
                })
            }
        } catch {
            continue
        }
    }
    return @($closed)
}

function Normalize-CodeXReportMessages {
    param(
        [Parameter(Mandatory = $true)][string] $WorkgroupRoot
    )

    $normalized = New-Object System.Collections.ArrayList
    $codexInbox = Join-Path $WorkgroupRoot 'inbox/CodeX'
    if (-not (Test-Path -LiteralPath $codexInbox -PathType Container)) {
        return @()
    }

    $messages = Get-ChildItem -LiteralPath $codexInbox -Filter '*.md' -File -ErrorAction SilentlyContinue
    foreach ($message in @($messages)) {
        try {
            $front = Read-AiwgFrontMatter -FilePath $message.FullName
            $id = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'id')
            $to = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'to')
            $type = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'type')
            $status = [string](Get-AiwgFrontMatterValue -FrontMatter $front -Key 'status')
            if ($to -eq 'CodeX' -and $type -eq 'report' -and $status -eq 'reported') {
                Set-AiwgFrontMatterValue -FilePath $message.FullName -Key 'status' -Value 'ready'
                Write-AiwgEvent -WorkgroupRoot $WorkgroupRoot -Agent 'Orchestrator' -Type 'codex_report_marked_ready' -MessageId $id -Path $message.FullName -Status 'ready'
                [void] $normalized.Add([pscustomobject]@{
                    id = $id
                    path = $message.FullName
                })
            }
        } catch {
            continue
        }
    }
    return @($normalized)
}

function Write-CoordinatorCursor {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)] $Cursor
    )
    New-Item -ItemType Directory -Force -Path (Split-Path $Path -Parent) | Out-Null
    [System.IO.File]::WriteAllText($Path, ($Cursor | ConvertTo-Json -Depth 8), [System.Text.UTF8Encoding]::new($false))
}

$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
$workgroupRoot = Join-Path $resolvedProjectRoot $WorkgroupRelativePath
$cursorPath = Join-Path $workgroupRoot 'state/plan-cursor.json'

if (-not (Test-Path -LiteralPath $workgroupRoot -PathType Container)) {
    throw "Workgroup root does not exist: $workgroupRoot"
}

$resolvedPhaseEnvelopePath = Resolve-PhaseEnvelopePath -ProjectRoot $resolvedProjectRoot -WorkgroupRoot $workgroupRoot -ExplicitPath $PhaseEnvelopePath
$phaseEnvelope = Read-PhaseEnvelope -Path $resolvedPhaseEnvelopePath

$normalizedCodeXReports = Normalize-CodeXReportMessages -WorkgroupRoot $workgroupRoot
$closedAnsweredCodeX = Complete-AnsweredCodeXMessages -WorkgroupRoot $workgroupRoot

$autopilot = Join-Path $PSScriptRoot 'Invoke-ProjectAutopilotOnce.ps1'
$autopilotParams = @{
    ProjectRoot = $resolvedProjectRoot
    WorkgroupRelativePath = $WorkgroupRelativePath
    MaxBudgetUsd = $MaxBudgetUsd
    TimeoutSeconds = $TimeoutSeconds
    Json = $true
}
if ($AllowWrite) {
    $autopilotParams['AllowWrite'] = $true
}

$autopilotOutput = & $autopilot @autopilotParams 2>&1
$autopilotExit = $LASTEXITCODE
$autopilotResult = $null
try {
    $autopilotResult = $autopilotOutput | ConvertFrom-Json
} catch {
    $autopilotResult = [pscustomobject]@{ raw = ($autopilotOutput -join "`n") }
}

if ($autopilotExit -ne 0) {
    $result = [pscustomobject]@{
        ok = $false
        action = 'claude_runner_failed'
        reason = ($autopilotOutput -join "`n")
        autopilot = $autopilotResult
    }
    Write-CoordinatorResult $result
    exit $autopilotExit
}

if ($autopilotResult.action -eq 'runner_completed') {
    $normalizedCodeXReports = Normalize-CodeXReportMessages -WorkgroupRoot $workgroupRoot
    $result = [pscustomobject]@{
        ok = $true
        action = 'claude_runner_completed'
        message_id = $autopilotResult.message_id
        normalized_codex_reports = @($normalizedCodeXReports)
        autopilot = $autopilotResult
    }
    Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent 'Orchestrator' -Type 'coordinator_claude_runner_completed' -MessageId $autopilotResult.message_id -Status 'ok'
    Write-CoordinatorResult $result
    exit 0
}

$messages = Get-WorkflowMessages -WorkgroupRoot $workgroupRoot
$humanBlocks = @($messages | Where-Object { $_.area -eq 'inbox' -and $_.status -eq 'ready' -and $_.requires_human -and $_.to -eq 'Human' })
$codexReady = @($messages | Where-Object { $_.area -eq 'inbox' -and $_.agent -eq 'CodeX' -and $_.status -eq 'ready' })
$claudeReady = @($messages | Where-Object { $_.area -eq 'inbox' -and $_.agent -eq 'Claude-Code' -and $_.status -eq 'ready' -and $_.can_write -and -not $_.requires_human })
$workingBlocks = @($messages | Where-Object {
    $_.area -eq 'working' -and $_.status -in @('ready', 'claimed', 'working', 'failed', 'stale_claim', 'needs_review', 'needs_clarification', 'needs_manual_recovery')
})
$locksDir = Join-Path $workgroupRoot 'state/locks'
$locks = @()
if (Test-Path -LiteralPath $locksDir -PathType Container) {
    $locks = @(Get-ChildItem -LiteralPath $locksDir -File -ErrorAction SilentlyContinue)
}

if ($humanBlocks.Count -gt 0) {
    $result = [pscustomobject]@{
        ok = $true
        action = 'waiting_human'
        reason = 'Human Gate has ready decisions.'
        pending = @($humanBlocks | Select-Object id, task, path)
        autopilot = $autopilotResult
    }
    Write-CoordinatorResult $result
    exit 0
}

if ($locks.Count -gt 0) {
    $result = [pscustomobject]@{
        ok = $true
        action = 'waiting_locks'
        reason = 'state/locks is not empty.'
        pending = @($locks | Select-Object Name, FullName)
        autopilot = $autopilotResult
    }
    Write-CoordinatorResult $result
    exit 0
}

if ($workingBlocks.Count -gt 0) {
    $result = [pscustomobject]@{
        ok = $true
        action = 'waiting_working'
        reason = 'working queue still has non-terminal items.'
        pending = @($workingBlocks | Select-Object id, task, status, path)
        autopilot = $autopilotResult
    }
    Write-CoordinatorResult $result
    exit 0
}

if ($claudeReady.Count -gt 0) {
    $result = [pscustomobject]@{
        ok = $true
        action = 'claude_ready_but_not_run'
        reason = 'Claude-Code has executable tasks, but AllowWrite was not set or autopilot did not consume them.'
        pending = @($claudeReady | Select-Object id, task, path)
        autopilot = $autopilotResult
    }
    Write-CoordinatorResult $result
    exit 0
}

if ($codexReady.Count -gt 0) {
    $result = [pscustomobject]@{
        ok = $true
        action = 'waiting_codex'
        reason = 'CodeX has ready tasks/reports to process; do not create duplicate next-phase triage.'
        pending = @($codexReady | Select-Object id, task, type, path)
        autopilot = $autopilotResult
    }
    Write-CoordinatorResult $result
    exit 0
}

$fingerprint = New-CoordinatorFingerprint -ProjectRoot $resolvedProjectRoot -WorkgroupRoot $workgroupRoot -PlanFiles $PlanFiles -PhaseEnvelopePath $resolvedPhaseEnvelopePath
$cursor = Read-CoordinatorCursor -Path $cursorPath
if ($cursor.last_fingerprint -eq $fingerprint -and -not [string]::IsNullOrWhiteSpace([string]$cursor.last_message_id)) {
    $result = [pscustomobject]@{
        ok = $true
        action = 'idle_already_dispatched'
        reason = 'Current plan/workgroup fingerprint already has a CodeX next-phase triage dispatch.'
        fingerprint = $fingerprint
        message_id = $cursor.last_message_id
        path = $cursor.last_message_path
        autopilot = $autopilotResult
    }
    Write-CoordinatorResult $result
    exit 0
}

if (-not $AllowWrite) {
    $result = [pscustomobject]@{
        ok = $true
        action = 'idle_dispatch_disabled'
        reason = 'Execution queue is idle, but AllowWrite was not set; no CodeX triage task was created.'
        fingerprint = $fingerprint
        autopilot = $autopilotResult
    }
    Write-CoordinatorResult $result
    exit 0
}

$contextFiles = @(Get-RecentContextFiles -ProjectRoot $resolvedProjectRoot -WorkgroupRoot $workgroupRoot -PlanFiles $PlanFiles)
$phaseEnvelopeContextFiles = @(Get-PhaseEnvelopeContextFiles -ProjectRoot $resolvedProjectRoot -Envelope $phaseEnvelope)
$contextFiles = @($contextFiles + $phaseEnvelopeContextFiles | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
$phaseEnvelopePrompt = Format-PhaseEnvelopeForPrompt -Envelope $phaseEnvelope
$task = 'AUTO-next-phase-triage'
$title = 'Auto continuation: next-phase triage and dispatch'
$body = @"
This task was created automatically by Orchestrator after the execution queue became idle.
CodeX should act as reviewer plus next-phase task splitter/dispatcher.

Default language requirement:

- Write user-facing replies, Human reports, and planning notes in Simplified Chinese.
- Keep code symbols, file paths, commands, status values, API names, and exact identifiers in English.

$phaseEnvelopePrompt

## Queue state

Orchestrator has confirmed:

- no executable ready Claude-Code write task;
- no claimed/failed/stale/non-terminal Claude-Code working item;
- no ready CodeX inbox item;
- no ready Human Gate decision with requires_human:true;
- state/locks is empty.

## CodeX responsibilities

Read context_files, including the current plan, recent Human decisions, CodeX review reports, and Claude-Code reports. Compare them with the current project/workgroup state, then decide the safest minimal next action.

Allowed:

- write a Chinese Human report summarizing current state and next recommendation;
- create one narrow Claude-Code task if the next step is clearly inside the approved phase envelope;
- write a Human decision report and wait if the next step touches a Human Gate.

Forbidden:

- do not edit business code directly;
- do not bypass Human Gate;
- do not create broad implementation tasks;
- do not touch pricing, payment, deployment, secrets, migration, real upload entrypoints, real preview media generation, clone providers, or Gateway/API wiring unless Human already approved it explicitly.

## Deduplication

coordinator_fingerprint: `$fingerprint`

If no new task is needed, write a Chinese Human report explaining why. Do not duplicate Claude-Code tasks.
"@

$messageScript = Join-Path $PSScriptRoot 'New-WorkgroupMessage.ps1'
$messageOutput = & $messageScript `
    -ProjectRoot $resolvedProjectRoot `
    -WorkgroupRelativePath $WorkgroupRelativePath `
    -Task $task `
    -From 'Orchestrator' `
    -To 'CodeX' `
    -Type 'instruction' `
    -Status 'ready' `
    -Priority 'high' `
    -RequiresHuman $false `
    -CanWrite $false `
    -ContextFiles $contextFiles `
    -AllowedFiles @() `
    -ForbiddenFiles @('.env', 'migrations/**', 'docs/ai-workgroup/state/**') `
    -MaxAttempts 1 `
    -TimeoutMinutes 30 `
    -ReviewDelegate 'CodeX' `
    -Title $title `
    -Body $body `
    -Json 2>&1
$messageExit = $LASTEXITCODE
if ($messageExit -ne 0) {
    $result = [pscustomobject]@{
        ok = $false
        action = 'dispatch_failed'
        reason = ($messageOutput -join "`n")
        fingerprint = $fingerprint
    }
    Write-CoordinatorResult $result
    exit $messageExit
}

$message = $messageOutput | ConvertFrom-Json
$newCursor = [ordered]@{
    project_root = $resolvedProjectRoot
    workgroup_root = $workgroupRoot
    plan_files = @($PlanFiles)
    last_fingerprint = $fingerprint
    last_dispatched_at = New-AiwgIsoTimestamp
    last_task = $task
    last_message_id = $message.id
    last_message_path = $message.path
}
Write-CoordinatorCursor -Path $cursorPath -Cursor $newCursor
Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent 'Orchestrator' -Type 'coordinator_next_phase_triage_created' -MessageId $message.id -Path $message.path -Status $fingerprint

$result = [pscustomobject]@{
    ok = $true
    action = 'next_phase_triage_created'
    message_id = $message.id
    path = $message.path
    fingerprint = $fingerprint
    context_files = @($contextFiles)
    autopilot = $autopilotResult
}
Write-CoordinatorResult $result
exit 0
