param(
    [Parameter(Mandatory = $true)]
    [string] $ProjectRoot,
    [string] $WorkgroupRelativePath = 'docs/ai-workgroup',
    [string[]] $Agents = @('CodeX', 'Claude-Code', 'OpenCode', 'Human'),
    [switch] $Json
)

$ErrorActionPreference = 'Stop'

$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
$workgroupRoot = Join-Path $resolvedProjectRoot $WorkgroupRelativePath

$created = New-Object System.Collections.ArrayList

function Ensure-Directory {
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
        [void] $created.Add($Path)
    }
}

Ensure-Directory $workgroupRoot
foreach ($agent in $Agents) {
    Ensure-Directory (Join-Path $workgroupRoot "inbox/$agent")
    Ensure-Directory (Join-Path $workgroupRoot "working/$agent")
}
Ensure-Directory (Join-Path $workgroupRoot 'done')
Ensure-Directory (Join-Path $workgroupRoot 'archive')
Ensure-Directory (Join-Path $workgroupRoot 'shared')
Ensure-Directory (Join-Path $workgroupRoot 'state')
Ensure-Directory (Join-Path $workgroupRoot 'state/locks')
Ensure-Directory (Join-Path $workgroupRoot 'status')

$result = [pscustomobject]@{
    project_root = $resolvedProjectRoot
    workgroup_root = $workgroupRoot
    agents = $Agents
    created = @($created)
}

if ($Json) {
    $result | ConvertTo-Json -Depth 6
} else {
    $result
}
