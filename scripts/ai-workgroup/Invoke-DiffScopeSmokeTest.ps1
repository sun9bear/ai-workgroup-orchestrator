param()

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).ProviderPath
$tmpRoot = Join-Path $repoRoot 'tests\tmp'
$projectRoot = Join-Path $tmpRoot 'diff-scope-smoke'

if ((Test-Path -LiteralPath $projectRoot) -and $projectRoot.StartsWith($tmpRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    Remove-Item -LiteralPath $projectRoot -Recurse -Force
}

New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'src'), (Join-Path $projectRoot 'gateway') | Out-Null
[System.IO.File]::WriteAllText((Join-Path $projectRoot 'src\app.txt'), "baseline`n", [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText((Join-Path $projectRoot 'gateway\secret.txt'), "baseline`n", [System.Text.UTF8Encoding]::new($false))

& git -C $projectRoot init -q
if ($LASTEXITCODE -ne 0) { throw 'git init failed' }
& git -C $projectRoot add -- .
if ($LASTEXITCODE -ne 0) { throw 'git add failed' }
& git -C $projectRoot -c user.email=aiwg@example.test -c user.name='AIWG Smoke' commit -q -m init
if ($LASTEXITCODE -ne 0) { throw 'git commit failed' }

New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot '.codegraph'), (Join-Path $projectRoot 'docs\audits') | Out-Null
[System.IO.File]::WriteAllText((Join-Path $projectRoot '.codegraph\daemon.pid'), "123`n", [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText((Join-Path $projectRoot 'docs\audits\preexisting.md'), "pre-existing untracked file`n", [System.Text.UTF8Encoding]::new($false))
$baselineFile = Join-Path $projectRoot 'docs\ai-workgroup\state\diagnostics\baseline-changed-paths.json'
New-Item -ItemType Directory -Force -Path (Split-Path $baselineFile -Parent) | Out-Null
@('.codegraph/daemon.pid', 'docs/audits/preexisting.md') |
    ConvertTo-Json |
    Set-Content -LiteralPath $baselineFile -Encoding UTF8

$messageJson = & (Join-Path $PSScriptRoot 'New-WorkgroupMessage.ps1') `
    -ProjectRoot $projectRoot `
    -Task 'DS0-diff-scope-smoke' `
    -From CodeX `
    -To Claude-Code `
    -Type instruction `
    -Status ready `
    -Priority medium `
    -RequiresHuman:$false `
    -CanWrite:$true `
    -AllowedFiles @('src/**') `
    -ForbiddenFiles @('gateway/**', '.env', 'migrations/**', 'docs/ai-workgroup/state/**') `
    -Title 'Diff scope smoke task' `
    -Body 'Only src/** may be changed.' `
    -Json 2>&1
$messageOk = $?
if (-not $messageOk) { throw "message creation failed: $messageJson" }
$message = $messageJson | ConvertFrom-Json

[System.IO.File]::WriteAllText((Join-Path $projectRoot 'src\app.txt'), "allowed change`n", [System.Text.UTF8Encoding]::new($false))
$okJson = & (Join-Path $PSScriptRoot 'Check-DiffScope.ps1') -ProjectRoot $projectRoot -MessagePath $message.path -BaselineFile $baselineFile -Json
if ($LASTEXITCODE -ne 0) { throw "expected allowed diff to pass: $okJson" }
$okResult = $okJson | ConvertFrom-Json
if (-not $okResult.ok) { throw "expected ok=true for allowed diff: $okJson" }

[System.IO.File]::WriteAllText((Join-Path $projectRoot 'gateway\secret.txt'), "forbidden change`n", [System.Text.UTF8Encoding]::new($false))
$badJson = & (Join-Path $PSScriptRoot 'Check-DiffScope.ps1') -ProjectRoot $projectRoot -MessagePath $message.path -BaselineFile $baselineFile -Json 2>&1
$badExit = $LASTEXITCODE
if ($badExit -eq 0) { throw "expected forbidden diff to fail: $badJson" }
$badResult = $badJson | ConvertFrom-Json
if ($badResult.ok) { throw "expected ok=false for forbidden diff: $badJson" }
if ('gateway/secret.txt' -notin @($badResult.forbidden_violations)) {
    throw "expected gateway/secret.txt forbidden violation: $badJson"
}

Write-Output 'OK diff scope smoke test passed.'
