param(
    [string] $ProjectRoot = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
Set-Location $resolvedRoot

$tmpRoot = Join-Path $resolvedRoot 'tests/tmp/anonymous-preview-handoff'
if ((Test-Path -LiteralPath $tmpRoot) -and -not ((Resolve-Path -LiteralPath $tmpRoot).ProviderPath.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase))) {
    throw "Refusing to clean temp path outside project: $tmpRoot"
}
Remove-Item -LiteralPath $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue

$targetRoot = Join-Path $tmpRoot 'target-project'
$planDir = Join-Path $targetRoot 'docs/plans'
New-Item -ItemType Directory -Force -Path $planDir, (Join-Path $targetRoot 'frontend-next/src'), (Join-Path $targetRoot 'gateway') | Out-Null

$planPath = Join-Path $planDir '2026-06-01-anonymous-preview-funnel-ux-plan.md'
[System.IO.File]::WriteAllText($planPath, '# Anonymous Preview Funnel Plan Smoke', [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText((Join-Path $targetRoot 'CLAUDE.md'), '# CLAUDE', [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText((Join-Path $targetRoot 'AGENTS.md'), '# AGENTS', [System.Text.UTF8Encoding]::new($false))

$resultJson = & 'scripts/ai-workgroup/New-AnonymousPreviewFunnelHandoff.ps1' -TargetProjectRoot $targetRoot -PlanPath $planPath -Json
$result = $resultJson | ConvertFrom-Json

if ($result.messages.Count -lt 2) {
    throw "Expected at least two generated messages. Output: $resultJson"
}

foreach ($message in $result.messages) {
    if (-not (Test-Path -LiteralPath $message.path -PathType Leaf)) {
        throw "Generated message was not found: $($message.path)"
    }
    $validation = & 'scripts/ai-workgroup/validate-message.ps1' -Path $message.path 2>&1
    if (-not $?) {
        throw "Message validation failed for $($message.path): $($validation -join "`n")"
    }
}

if (-not (Test-Path -LiteralPath $result.next_action -PathType Leaf)) {
    throw "NEXT_ACTION.md was not generated: $($result.next_action)"
}

[pscustomobject]@{
    Passed = $true
    WorkgroupRoot = $result.workgroup_root
    Messages = @($result.messages | ForEach-Object { $_.path })
    NextAction = $result.next_action
}
