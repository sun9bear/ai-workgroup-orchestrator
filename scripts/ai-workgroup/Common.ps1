$ErrorActionPreference = 'Stop'

function New-AiwgIsoTimestamp {
    return (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
}

function Convert-AiwgScalar {
    param([string] $Value)
    if ($null -eq $Value) {
        return ''
    }
    $trimmed = $Value.Trim()
    if (($trimmed.StartsWith('"') -and $trimmed.EndsWith('"')) -or
        ($trimmed.StartsWith("'") -and $trimmed.EndsWith("'"))) {
        return $trimmed.Substring(1, $trimmed.Length - 2)
    }
    return $trimmed
}

function Read-AiwgFrontMatter {
    param([Parameter(Mandatory = $true)][string] $FilePath)

    $lines = [System.IO.File]::ReadAllLines($FilePath, [System.Text.Encoding]::UTF8)
    if ($lines.Count -lt 3 -or $lines[0].Trim() -ne '---') {
        throw "Missing opening front matter delimiter in $FilePath"
    }

    $endIndex = -1
    for ($i = 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i].Trim() -eq '---') {
            $endIndex = $i
            break
        }
    }
    if ($endIndex -lt 0) {
        throw "Missing closing front matter delimiter in $FilePath"
    }

    $data = [ordered]@{}
    $currentKey = $null

    for ($i = 1; $i -lt $endIndex; $i++) {
        $line = $lines[$i]
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) {
            continue
        }

        if ($line -match '^\s+-\s*(.*)$') {
            if ([string]::IsNullOrWhiteSpace($currentKey)) {
                throw "List item without parent key at front matter line $($i + 1)."
            }
            if ($data[$currentKey] -isnot [System.Collections.IList] -or $data[$currentKey] -is [string]) {
                $data[$currentKey] = New-Object System.Collections.ArrayList
            }
            [void] $data[$currentKey].Add((Convert-AiwgScalar $Matches[1]))
            continue
        }

        if ($line -match '^\s+([A-Za-z0-9_-]+):\s*(.*)$') {
            if ([string]::IsNullOrWhiteSpace($currentKey)) {
                throw "Nested value without parent key at front matter line $($i + 1)."
            }
            if ($data[$currentKey] -isnot [System.Collections.IDictionary]) {
                $data[$currentKey] = [ordered]@{}
            }
            $data[$currentKey][$Matches[1]] = Convert-AiwgScalar $Matches[2]
            continue
        }

        if ($line -match '^([A-Za-z0-9_-]+):\s*(.*)$') {
            $currentKey = $Matches[1]
            $value = $Matches[2]
            if ([string]::IsNullOrWhiteSpace($value)) {
                $data[$currentKey] = ''
            } elseif ($value.Trim() -eq '[]') {
                $data[$currentKey] = @()
            } else {
                $data[$currentKey] = Convert-AiwgScalar $value
            }
            continue
        }

        throw "Unsupported front matter syntax at line $($i + 1): $line"
    }

    return $data
}

function Get-AiwgFrontMatterValue {
    param(
        [Parameter(Mandatory = $true)] $FrontMatter,
        [Parameter(Mandatory = $true)][string] $Key,
        [object] $Default = ''
    )
    if ($FrontMatter.Contains($Key)) {
        return $FrontMatter[$Key]
    }
    return $Default
}

function ConvertTo-AiwgList {
    param($Value)
    if ($null -eq $Value) {
        return @()
    }
    if ($Value -is [System.Collections.IList] -and -not ($Value -is [string])) {
        return @($Value)
    }
    if ([string]::IsNullOrWhiteSpace([string]$Value)) {
        return @()
    }
    return @([string]$Value)
}

function ConvertTo-AiwgBool {
    param($Value)
    return ([string]$Value).Trim().ToLowerInvariant() -eq 'true'
}

function Set-AiwgFrontMatterValue {
    param(
        [Parameter(Mandatory = $true)][string] $FilePath,
        [Parameter(Mandatory = $true)][string] $Key,
        [Parameter(Mandatory = $true)][string] $Value
    )

    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($line in [System.IO.File]::ReadAllLines($FilePath, [System.Text.Encoding]::UTF8)) {
        $lines.Add($line)
    }

    if ($lines.Count -lt 2 -or $lines[0].Trim() -ne '---') {
        throw "File does not have YAML front matter: $FilePath"
    }

    $endIndex = -1
    for ($i = 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i].Trim() -eq '---') {
            $endIndex = $i
            break
        }
    }
    if ($endIndex -lt 0) {
        throw "File front matter is not closed: $FilePath"
    }

    $updated = $false
    for ($i = 1; $i -lt $endIndex; $i++) {
        if ($lines[$i] -match "^$([regex]::Escape($Key)):\s*") {
            $lines[$i] = "${Key}: $Value"
            $updated = $true
            break
        }
    }
    if (-not $updated) {
        $lines.Insert($endIndex, "${Key}: $Value")
    }

    [System.IO.File]::WriteAllLines($FilePath, $lines, [System.Text.UTF8Encoding]::new($false))
}

function ConvertTo-AiwgRepoPath {
    param([string] $Path)
    return (($Path -replace '\\', '/') -replace '^\./', '').Trim()
}

function Test-AiwgPathPattern {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)][string] $Pattern
    )

    $normalizedPath = ConvertTo-AiwgRepoPath $Path
    $normalizedPattern = ConvertTo-AiwgRepoPath $Pattern
    if ($normalizedPath -eq $normalizedPattern) {
        return $true
    }
    return $normalizedPath -like $normalizedPattern
}

function Add-AiwgJsonLine {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)] $Object,
        [int] $MaxAttempts = 5
    )

    New-Item -ItemType Directory -Force -Path (Split-Path $Path -Parent) | Out-Null
    $json = $Object | ConvertTo-Json -Compress -Depth 8
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
            try {
                $writer = [System.IO.StreamWriter]::new($stream, [System.Text.UTF8Encoding]::new($false))
                $writer.WriteLine($json)
                $writer.Flush()
            } finally {
                if ($null -ne $writer) { $writer.Dispose() }
                if ($null -ne $stream) { $stream.Dispose() }
            }
            return
        } catch {
            if ($attempt -eq $MaxAttempts) {
                throw
            }
            Start-Sleep -Milliseconds (100 * $attempt)
        }
    }
}

function Write-AiwgEvent {
    param(
        [Parameter(Mandatory = $true)][string] $WorkgroupRoot,
        [Parameter(Mandatory = $true)][string] $Agent,
        [Parameter(Mandatory = $true)][string] $Type,
        [string] $MessageId = '',
        [string] $Path = '',
        [string] $Status = ''
    )

    $event = [ordered]@{
        type = $Type
        agent = $Agent
        message_id = $MessageId
        at = (New-AiwgIsoTimestamp)
    }
    if (-not [string]::IsNullOrWhiteSpace($Path)) {
        $event.path = $Path
    }
    if (-not [string]::IsNullOrWhiteSpace($Status)) {
        $event.status = $Status
    }
    $eventsPath = Join-Path $WorkgroupRoot "state/events.$Agent.jsonl"
    Add-AiwgJsonLine -Path $eventsPath -Object $event
}
