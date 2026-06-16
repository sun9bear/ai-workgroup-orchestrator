param(
    [string] $ProjectRoot = 'D:\example\protected-business-repo',
    [string] $WorkgroupRelativePath = 'docs/ai-workgroup',
    [string] $HostName = '127.0.0.1',
    [int] $Port = 8765,
    [switch] $NoBrowser,
    [switch] $Once
)

$ErrorActionPreference = 'Stop'

$server = Join-Path $PSScriptRoot 'human-dashboard-server.py'
if (-not (Test-Path -LiteralPath $server -PathType Leaf)) {
    throw "Dashboard server not found: $server"
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $python) {
    throw 'Python was not found on PATH.'
}

$argsList = @(
    $server,
    '--project-root', $ProjectRoot,
    '--workgroup-relative-path', $WorkgroupRelativePath,
    '--host', $HostName,
    '--port', ([string]$Port)
)
if ($Once) {
    $argsList += '--once'
}

$url = "http://${HostName}:$Port"
if (-not $Once -and -not $NoBrowser) {
    Start-Process -FilePath $url | Out-Null
}

Write-Output "AI Workgroup Dashboard: $url"
& $python.Source @argsList
