param()

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).ProviderPath
$tmpRoot = Join-Path $repoRoot 'tests\tmp'
$projectRoot = Join-Path $tmpRoot 'claude-implementation-dry-run-smoke'

if ((Test-Path -LiteralPath $projectRoot) -and $projectRoot.StartsWith($tmpRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    Remove-Item -LiteralPath $projectRoot -Recurse -Force
}

New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'src') | Out-Null
[System.IO.File]::WriteAllText((Join-Path $projectRoot 'src\app.txt'), "baseline`n", [System.Text.UTF8Encoding]::new($false))

$messageJson = & (Join-Path $PSScriptRoot 'New-WorkgroupMessage.ps1') `
    -ProjectRoot $projectRoot `
    -Task 'CI0-claude-implementation-dry-run' `
    -From CodeX `
    -To Claude-Code `
    -Type instruction `
    -Status ready `
    -Priority medium `
    -RequiresHuman:$false `
    -CanWrite:$true `
    -AllowedFiles @('src/**') `
    -ForbiddenFiles @('.env', 'migrations/**', 'docs/ai-workgroup/state/**') `
    -Title 'Claude implementation dry-run smoke task' `
    -Body 'Dry run should select this task but must not call Claude.' `
    -Json 2>&1
$messageOk = $?
if (-not $messageOk) { throw "message creation failed: $messageJson" }
$message = $messageJson | ConvertFrom-Json

$dryRunJson = & (Join-Path $PSScriptRoot 'Invoke-ClaudeImplementationTask.ps1') `
    -ProjectRoot $projectRoot `
    -MessageId $message.id `
    -DryRun `
    -Json
if ($LASTEXITCODE -ne 0) { throw "dry run failed: $dryRunJson" }
$dryRun = $dryRunJson | ConvertFrom-Json

if (-not $dryRun.ok) { throw "expected dry run ok=true: $dryRunJson" }
if ($dryRun.action -ne 'dry_run') { throw "expected dry_run action: $dryRunJson" }
if (-not $dryRun.would_call_claude) { throw "expected would_call_claude=true: $dryRunJson" }

$frontMatter = [System.IO.File]::ReadAllText($message.path, [System.Text.Encoding]::UTF8)
if ($frontMatter -notmatch 'status:\s*ready') {
    throw 'dry run should not claim or modify the task status.'
}

Write-Output 'OK Claude implementation dry-run smoke test passed.'
