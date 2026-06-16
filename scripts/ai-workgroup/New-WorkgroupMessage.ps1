param(
    [Parameter(Mandatory = $true)]
    [string] $ProjectRoot,
    [string] $WorkgroupRelativePath = 'docs/ai-workgroup',
    [Parameter(Mandatory = $true)]
    [string] $Task,
    [Parameter(Mandatory = $true)]
    [string] $From,
    [Parameter(Mandatory = $true)]
    [string] $To,
    [string] $Type = 'instruction',
    [string] $Status = 'ready',
    [string] $Priority = 'medium',
    [string] $ReplyTo = '',
    [bool] $RequiresHuman = $false,
    [bool] $CanWrite = $false,
    [string[]] $ContextFiles = @(),
    [string[]] $AllowedFiles = @(),
    [string[]] $ForbiddenFiles = @('.env', 'migrations/**', 'docs/ai-workgroup/state/**'),
    [int] $Attempt = 0,
    [int] $MaxAttempts = 1,
    [int] $TimeoutMinutes = 30,
    [string] $ReviewDelegate = 'CodeX',
    [Parameter(Mandatory = $true)]
    [string] $Title,
    [Parameter(Mandatory = $true)]
    [string] $Body,
    [switch] $SkipValidation,
    [switch] $Json
)

$ErrorActionPreference = 'Stop'

function New-IsoTimestamp {
    return (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
}

function New-FileTimestamp {
    return (Get-Date).ToString("yyyy-MM-ddTHHmmss")
}

function ConvertTo-SafeFileName {
    param([string] $Value)
    return ($Value -replace '[^A-Za-z0-9_.-]', '_')
}

function Format-FrontMatterList {
    param(
        [string] $Key,
        [string[]] $Values
    )
    if ($null -eq $Values -or $Values.Count -eq 0) {
        return "${Key}: []"
    }
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("${Key}:")
    foreach ($value in $Values) {
        $escaped = ([string]$value).Replace('"', '\"')
        $lines.Add("  - $escaped")
    }
    return ($lines -join "`n")
}

if (-not $CanWrite -and $AllowedFiles.Count -gt 0) {
    throw 'AllowedFiles must be empty when CanWrite is false.'
}
if ($CanWrite -and $AllowedFiles.Count -eq 0) {
    throw 'AllowedFiles is required when CanWrite is true.'
}

$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
$workgroupRoot = Join-Path $resolvedProjectRoot $WorkgroupRelativePath
$inbox = Join-Path $workgroupRoot "inbox/$To"
New-Item -ItemType Directory -Force -Path $inbox | Out-Null

$createdAt = New-IsoTimestamp
$timestamp = New-FileTimestamp
$safeTask = ConvertTo-SafeFileName $Task
$messageId = "$safeTask-msg-$timestamp"
$fileName = "${timestamp}_from-$From`_to-$To`_type-$Type`_task-$safeTask.md"
$messagePath = Join-Path $inbox $fileName

$requiresHumanText = if ($RequiresHuman) { 'true' } else { 'false' }
$canWriteText = if ($CanWrite) { 'true' } else { 'false' }
$contextBlock = Format-FrontMatterList -Key 'context_files' -Values $ContextFiles
$allowedBlock = Format-FrontMatterList -Key 'allowed_files' -Values $AllowedFiles
$forbiddenBlock = Format-FrontMatterList -Key 'forbidden_files' -Values $ForbiddenFiles

$content = @"
---
id: $messageId
task: $Task
from: $From
to: $To
type: $Type
status: $Status
priority: $Priority
reply_to: "$ReplyTo"
requires_human: $requiresHumanText
created_at: $createdAt
can_write: $canWriteText
$contextBlock
$allowedBlock
$forbiddenBlock
attempt: $Attempt
max_attempts: $MaxAttempts
timeout_minutes: $TimeoutMinutes
review_delegate: $ReviewDelegate
---

# $Title

$Body
"@

[System.IO.File]::WriteAllText($messagePath, $content, [System.Text.UTF8Encoding]::new($false))

if (-not $SkipValidation) {
    $validator = Join-Path $PSScriptRoot 'validate-message.ps1'
    if (Test-Path -LiteralPath $validator -PathType Leaf) {
        $validation = & $validator -Path $messagePath 2>&1
        if (-not $?) {
            throw "Generated message failed validation: $($validation -join "`n")"
        }
    }
}

$result = [pscustomobject]@{
    id = $messageId
    task = $Task
    from = $From
    to = $To
    path = $messagePath
}

if ($Json) {
    $result | ConvertTo-Json -Depth 6
} else {
    $result
}
