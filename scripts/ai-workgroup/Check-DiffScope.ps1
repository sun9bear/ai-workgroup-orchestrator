param(
    [Parameter(Mandatory = $true)]
    [string] $ProjectRoot,
    [Parameter(Mandatory = $true)]
    [string] $MessagePath,
    [string] $BaseRef = 'HEAD',
    [string] $BaselineFile = '',
    [string[]] $IgnoreFiles = @('docs/ai-workgroup/**'),
    [switch] $Json
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

function Invoke-GitLines {
    param(
        [Parameter(Mandatory = $true)][string] $Root,
        [Parameter(Mandatory = $true)][string[]] $Arguments
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & git -C $Root @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code ${exitCode}: $($output -join "`n")"
    }
    return @($output | Where-Object {
        $line = [string]$_
        -not [string]::IsNullOrWhiteSpace($line) -and -not $line.StartsWith('warning:')
    })
}

function Test-AnyPattern {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [string[]] $Patterns
    )

    foreach ($pattern in @($Patterns)) {
        if ([string]::IsNullOrWhiteSpace($pattern)) {
            continue
        }
        if (Test-AiwgPathPattern -Path $Path -Pattern $pattern) {
            return $true
        }
    }
    return $false
}

function New-ScopeResult {
    param(
        [bool] $Ok,
        [string] $Reason,
        [string[]] $ChangedPaths,
        [string[]] $AllChangedPaths,
        [string[]] $BaselineIgnoredPaths,
        [string[]] $IgnoredPaths,
        [string[]] $AllowedPaths,
        [string[]] $OutsideAllowed,
        [string[]] $ForbiddenViolations,
        [string[]] $AllowedFiles,
        [string[]] $ForbiddenFiles,
        [string[]] $IgnorePatterns
    )

    return [pscustomobject]@{
        ok = $Ok
        reason = $Reason
        project_root = $resolvedProjectRoot
        message_path = $resolvedMessagePath
        base_ref = $BaseRef
        baseline_file = $BaselineFile
        all_changed_paths = @($AllChangedPaths)
        changed_paths = @($ChangedPaths)
        baseline_ignored_paths = @($BaselineIgnoredPaths)
        ignored_paths = @($IgnoredPaths)
        allowed_paths = @($AllowedPaths)
        outside_allowed = @($OutsideAllowed)
        forbidden_violations = @($ForbiddenViolations)
        violations = @(@($OutsideAllowed) + @($ForbiddenViolations) | Select-Object -Unique)
        allowed_files = @($AllowedFiles)
        forbidden_files = @($ForbiddenFiles)
        ignore_patterns = @($IgnorePatterns)
    }
}

function Write-Result {
    param($Result)
    if ($Json) {
        $Result | ConvertTo-Json -Depth 8
    } else {
        if ($Result.ok) {
            Write-Output "OK diff scope: $($Result.changed_paths.Count) changed, $($Result.ignored_paths.Count) ignored."
        } else {
            Write-Output "ERR diff scope: $($Result.reason)"
            foreach ($violation in $Result.violations) {
                Write-Output "  - $violation"
            }
        }
    }
}

$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
$resolvedMessagePath = (Resolve-Path -LiteralPath $MessagePath).ProviderPath
$frontMatter = Read-AiwgFrontMatter -FilePath $resolvedMessagePath

$canWrite = ConvertTo-AiwgBool (Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'can_write' -Default 'false')
$allowedFiles = ConvertTo-AiwgList (Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'allowed_files' -Default @())
$forbiddenFiles = ConvertTo-AiwgList (Get-AiwgFrontMatterValue -FrontMatter $frontMatter -Key 'forbidden_files' -Default @())
$baselinePaths = @()
if (-not [string]::IsNullOrWhiteSpace($BaselineFile)) {
    $resolvedBaselineFile = (Resolve-Path -LiteralPath $BaselineFile).ProviderPath
    $baselineText = [System.IO.File]::ReadAllText($resolvedBaselineFile, [System.Text.Encoding]::UTF8)
    if (-not [string]::IsNullOrWhiteSpace($baselineText)) {
        $baselineData = $baselineText | ConvertFrom-Json
        if ($baselineData -is [System.Collections.IEnumerable] -and -not ($baselineData -is [string])) {
            $baselinePaths = @($baselineData | ForEach-Object { ConvertTo-AiwgRepoPath ([string]$_) })
        } else {
            $baselinePaths = @((ConvertTo-AiwgRepoPath ([string]$baselineData)))
        }
    }
}

try {
    [void](Invoke-GitLines -Root $resolvedProjectRoot -Arguments @('rev-parse', '--is-inside-work-tree'))
    $tracked = Invoke-GitLines -Root $resolvedProjectRoot -Arguments @('diff', '--name-only', '--diff-filter=ACMRTUXB', $BaseRef, '--')
    $untracked = Invoke-GitLines -Root $resolvedProjectRoot -Arguments @('ls-files', '--others', '--exclude-standard')
} catch {
    $result = New-ScopeResult `
        -Ok $false `
        -Reason "git_unavailable: $($_.Exception.Message)" `
        -ChangedPaths @() `
        -AllChangedPaths @() `
        -BaselineIgnoredPaths @() `
        -IgnoredPaths @() `
        -AllowedPaths @() `
        -OutsideAllowed @() `
        -ForbiddenViolations @() `
        -AllowedFiles $allowedFiles `
        -ForbiddenFiles $forbiddenFiles `
        -IgnorePatterns $IgnoreFiles
    Write-Result $result
    exit 2
}

$allChangedPaths = @(@($tracked) + @($untracked) |
    ForEach-Object { ConvertTo-AiwgRepoPath ([string]$_) } |
    Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
    Sort-Object -Unique)

$baselineIgnoredPaths = New-Object System.Collections.ArrayList
$changedPathsForScope = New-Object System.Collections.ArrayList
foreach ($path in $allChangedPaths) {
    if ($path -in $baselinePaths) {
        [void] $baselineIgnoredPaths.Add($path)
    } else {
        [void] $changedPathsForScope.Add($path)
    }
}

$ignoredPaths = New-Object System.Collections.ArrayList
$effectiveChangedPaths = New-Object System.Collections.ArrayList
foreach ($path in @($changedPathsForScope)) {
    if (Test-AnyPattern -Path $path -Patterns $IgnoreFiles) {
        [void] $ignoredPaths.Add($path)
    } else {
        [void] $effectiveChangedPaths.Add($path)
    }
}

$allowedPaths = New-Object System.Collections.ArrayList
$outsideAllowed = New-Object System.Collections.ArrayList
$forbiddenViolations = New-Object System.Collections.ArrayList

foreach ($path in @($effectiveChangedPaths)) {
    if (Test-AnyPattern -Path $path -Patterns $forbiddenFiles) {
        [void] $forbiddenViolations.Add($path)
        continue
    }

    if (-not $canWrite) {
        [void] $outsideAllowed.Add($path)
        continue
    }

    if (Test-AnyPattern -Path $path -Patterns $allowedFiles) {
        [void] $allowedPaths.Add($path)
    } else {
        [void] $outsideAllowed.Add($path)
    }
}

$violations = @(@($outsideAllowed) + @($forbiddenViolations) | Select-Object -Unique)
$ok = ($violations.Count -eq 0)
$reason = if ($ok) { 'ok' } else { 'scope_violation' }

$result = New-ScopeResult `
    -Ok $ok `
    -Reason $reason `
    -ChangedPaths @($changedPathsForScope) `
    -AllChangedPaths $allChangedPaths `
    -BaselineIgnoredPaths @($baselineIgnoredPaths) `
    -IgnoredPaths @($ignoredPaths) `
    -AllowedPaths @($allowedPaths) `
    -OutsideAllowed @($outsideAllowed) `
    -ForbiddenViolations @($forbiddenViolations) `
    -AllowedFiles $allowedFiles `
    -ForbiddenFiles $forbiddenFiles `
    -IgnorePatterns $IgnoreFiles

Write-Result $result
if ($ok) {
    exit 0
}
exit 1
