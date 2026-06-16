param(
    [Parameter(Mandatory = $true)]
    [string] $ProjectRoot,
    [string] $WorkgroupRelativePath = 'docs/ai-workgroup',
    [string] $PhaseEnvelopePath = '',
    [decimal] $MaxBudgetUsd = 3.00,
    [int] $TimeoutSeconds = 1200,
    [switch] $AllowWrite,
    [string] $LogRoot = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
$workgroupRoot = Join-Path $resolvedProjectRoot $WorkgroupRelativePath
if ([string]::IsNullOrWhiteSpace($LogRoot)) {
    $LogRoot = Join-Path $workgroupRoot 'state/logs'
}
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null

$startedAt = New-AiwgIsoTimestamp
$safeStamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
$logPath = Join-Path $LogRoot "project-autopilot-$safeStamp.log"
$runner = Join-Path $PSScriptRoot 'Invoke-ProjectCoordinatorOnce.ps1'

function Write-LogLine {
    param([string] $Line)
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value $Line
}

Write-LogLine "started_at=$startedAt"
Write-LogLine "project_root=$resolvedProjectRoot"
Write-LogLine "allow_write=$([bool]$AllowWrite)"
Write-LogLine "max_budget_usd=$MaxBudgetUsd"
Write-LogLine "timeout_seconds=$TimeoutSeconds"

try {
    $runnerParams = @{
        ProjectRoot = $resolvedProjectRoot
        WorkgroupRelativePath = $WorkgroupRelativePath
        MaxBudgetUsd = $MaxBudgetUsd
        TimeoutSeconds = $TimeoutSeconds
        Json = $true
    }
    if (-not [string]::IsNullOrWhiteSpace($PhaseEnvelopePath)) {
        $runnerParams['PhaseEnvelopePath'] = $PhaseEnvelopePath
    }
    if ($AllowWrite) {
        $runnerParams['AllowWrite'] = $true
    }

    Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent 'Orchestrator' -Type 'project_coordinator_scheduled_started' -Status "allow_write=$([bool]$AllowWrite)"
    $output = & $runner @runnerParams 2>&1
    $exitCode = $LASTEXITCODE
    Write-LogLine '--- output ---'
    foreach ($line in @($output)) {
        Write-LogLine ([string]$line)
    }
    Write-LogLine "exit_code=$exitCode"

    if ($exitCode -ne 0) {
        Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent 'Orchestrator' -Type 'project_coordinator_scheduled_failed' -Path $logPath -Status "exit_$exitCode"
        exit $exitCode
    }

    Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent 'Orchestrator' -Type 'project_coordinator_scheduled_finished' -Path $logPath -Status 'ok'
    exit 0
} catch {
    Write-LogLine "error=$($_.Exception.Message)"
    Write-AiwgEvent -WorkgroupRoot $workgroupRoot -Agent 'Orchestrator' -Type 'project_coordinator_scheduled_failed' -Path $logPath -Status $_.Exception.Message
    throw
}
