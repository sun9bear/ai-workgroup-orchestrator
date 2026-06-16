param(
    [Parameter(Mandatory = $true)]
    [string] $ProjectRoot,
    [string] $WorkgroupRelativePath = 'docs/ai-workgroup',
    [switch] $AllowWrite,
    [string] $MessageId = '',
    [decimal] $MaxBudgetUsd = 3.00,
    [int] $TimeoutSeconds = 900,
    [switch] $Json
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

function Get-ReadyMessages {
    param(
        [Parameter(Mandatory = $true)][string] $InboxPath,
        [string] $WantedMessageId = ''
    )

    $items = New-Object System.Collections.ArrayList
    $messages = Get-ChildItem -LiteralPath $InboxPath -Filter '*.md' -File -ErrorAction SilentlyContinue |
        Sort-Object Name

    foreach ($message in @($messages)) {
        try {
            $frontMatter = Read-AiwgFrontMatter -FilePath $message.FullName
            $id = [string](Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'id')
            $status = [string](Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'status')
            if (-not [string]::IsNullOrWhiteSpace($WantedMessageId) -and $id -ne $WantedMessageId) {
                continue
            }
            if ($status -ne 'ready') {
                continue
            }
            [void] $items.Add([pscustomobject]@{
                id = $id
                path = $message.FullName
                task = [string](Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'task')
                from = [string](Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'from')
                to = [string](Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'to')
                can_write = ConvertTo-AiwgBool (Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'can_write' -Default 'false')
                requires_human = ConvertTo-AiwgBool (Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'requires_human' -Default 'false')
            })
        } catch {
            [void] $items.Add([pscustomobject]@{
                id = ''
                path = $message.FullName
                task = ''
                from = ''
                to = ''
                can_write = $false
                requires_human = $true
                error = $_.Exception.Message
            })
        }
    }
    return @($items)
}

function Write-Result {
    param($Result)
    if ($Json) {
        $Result | ConvertTo-Json -Depth 8
    } else {
        Write-Output "Autopilot action: $($Result.action)"
        if ($Result.message_id) {
            Write-Output "Message: $($Result.message_id)"
        }
        if ($Result.reason) {
            Write-Output "Reason: $($Result.reason)"
        }
        if ($Result.pending) {
            foreach ($item in @($Result.pending)) {
                Write-Output "- $($item.to) $($item.id) can_write=$($item.can_write) requires_human=$($item.requires_human)"
            }
        }
    }
}

$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
$workgroupRoot = Join-Path $resolvedProjectRoot $WorkgroupRelativePath
$claudeInbox = Join-Path $workgroupRoot 'inbox/Claude-Code'
$codexInbox = Join-Path $workgroupRoot 'inbox/CodeX'

$pendingClaude = Get-ReadyMessages -InboxPath $claudeInbox -WantedMessageId $MessageId
$pendingCodex = Get-ReadyMessages -InboxPath $codexInbox
$pending = @(@($pendingClaude) + @($pendingCodex))

if (-not $AllowWrite) {
    $result = [pscustomobject]@{
        ok = $true
        action = 'observe_only'
        reason = 'AllowWrite was not set; no agent was invoked.'
        pending = @($pending)
    }
    Write-Result $result
    exit 0
}

$writeCandidates = @($pendingClaude | Where-Object { $_.can_write -and -not $_.requires_human })
if ($writeCandidates.Count -eq 0) {
    $result = [pscustomobject]@{
        ok = $true
        action = 'no_write_candidate'
        reason = 'No ready Claude-Code can_write=true task without Human Gate.'
        pending = @($pending)
    }
    Write-Result $result
    exit 0
}

$selected = $writeCandidates | Select-Object -First 1
Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent 'Orchestrator' -Type 'autopilot_dispatch' -MessageId $selected.id -Path $selected.path -Status 'Claude-Code'

$runner = Join-Path $PSScriptRoot 'Invoke-ClaudeImplementationTask.ps1'
$runnerOutput = & $runner `
    -ProjectRoot $resolvedProjectRoot `
    -WorkgroupRelativePath $WorkgroupRelativePath `
    -MessageId $selected.id `
    -MaxBudgetUsd $MaxBudgetUsd `
    -TimeoutSeconds $TimeoutSeconds `
    -Json 2>&1
$runnerExitCode = $LASTEXITCODE

if ($runnerExitCode -ne 0) {
    $result = [pscustomobject]@{
        ok = $false
        action = 'runner_failed'
        message_id = $selected.id
        reason = ($runnerOutput -join "`n")
    }
    Write-Result $result
    exit $runnerExitCode
}

$parsed = $null
try {
    $parsed = $runnerOutput | ConvertFrom-Json
} catch {
    $parsed = [pscustomobject]@{ raw = ($runnerOutput -join "`n") }
}

$result = [pscustomobject]@{
    ok = $true
    action = 'runner_completed'
    message_id = $selected.id
    runner_result = $parsed
}
Write-Result $result
exit 0
