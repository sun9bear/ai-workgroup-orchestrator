param(
    [Parameter(Mandatory = $true, ValueFromPipeline = $true, ValueFromPipelineByPropertyName = $true)]
    [string[]] $Path,

    [switch] $Json
)

begin {
    $ErrorActionPreference = 'Stop'
    $validAgents = @('CodeX', 'Claude-Code', 'Reviewer', 'Git-Steward', 'OpenCode', 'Pi', 'Fake', 'Human', 'Orchestrator')
    $validTypes = @('instruction', 'report', 'review', 'decision', 'blocker', 'ack', 'completion-report', 'advisory', 'advisory_report')
    $validStatuses = @(
        'ready',
        'claimed',
        'working',
        'reported',
        'reviewing',
        'needs_revision',
        'needs_review',
        'needs_clarification',
        'waiting_human',
        'waiting_codex',
        'review_degraded',
        'stale_claim',
        'needs_manual_recovery',
        'approved',
        'done',
        'cancelled',
        'failed',
        'archived'
    )
    $validPriorities = @('high', 'medium', 'low')
    $requiredFields = @(
        'id',
        'task',
        'from',
        'to',
        'type',
        'status',
        'priority',
        'requires_human',
        'created_at',
        'can_write'
    )

    function Convert-Scalar {
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

    function Ensure-List {
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

    function Read-MessageFrontMatter {
        param([string] $FilePath)

        $lines = [System.IO.File]::ReadAllLines($FilePath, [System.Text.Encoding]::UTF8)
        if ($lines.Count -lt 3 -or $lines[0].Trim() -ne '---') {
            throw 'Missing opening front matter delimiter.'
        }

        $endIndex = -1
        for ($i = 1; $i -lt $lines.Count; $i++) {
            if ($lines[$i].Trim() -eq '---') {
                $endIndex = $i
                break
            }
        }
        if ($endIndex -lt 0) {
            throw 'Missing closing front matter delimiter.'
        }

        $data = [ordered]@{}
        $currentKey = $null

        for ($i = 1; $i -lt $endIndex; $i++) {
            $line = $lines[$i]
            if ([string]::IsNullOrWhiteSpace($line)) {
                continue
            }
            if ($line.TrimStart().StartsWith('#')) {
                continue
            }

            if ($line -match '^\s+-\s*(.*)$') {
                if ([string]::IsNullOrWhiteSpace($currentKey)) {
                    throw "List item without parent key at front matter line $($i + 1)."
                }
                if ($data[$currentKey] -isnot [System.Collections.IList] -or $data[$currentKey] -is [string]) {
                    $data[$currentKey] = New-Object System.Collections.ArrayList
                }
                [void] $data[$currentKey].Add((Convert-Scalar $Matches[1]))
                continue
            }

            if ($line -match '^\s+([A-Za-z0-9_-]+):\s*(.*)$') {
                if ([string]::IsNullOrWhiteSpace($currentKey)) {
                    throw "Nested value without parent key at front matter line $($i + 1)."
                }
                if ($data[$currentKey] -isnot [System.Collections.IDictionary]) {
                    $data[$currentKey] = [ordered]@{}
                }
                $data[$currentKey][$Matches[1]] = Convert-Scalar $Matches[2]
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
                    $data[$currentKey] = Convert-Scalar $value
                }
                continue
            }

            throw "Unsupported front matter syntax at line $($i + 1): $line"
        }

        return $data
    }

    function Test-BooleanString {
        param($Value)
        return ([string]$Value) -in @('true', 'false')
    }

    function ConvertTo-BoolStrict {
        param($Value)
        if (([string]$Value) -eq 'true') {
            return $true
        }
        if (([string]$Value) -eq 'false') {
            return $false
        }
        throw "Expected boolean string true/false, got '$Value'."
    }

    function Test-MessageFile {
        param([string] $FilePath)

        $errors = New-Object System.Collections.ArrayList
        try {
            if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
                [void] $errors.Add('File does not exist.')
                return [pscustomobject]@{ Path = $FilePath; Valid = $false; Errors = @($errors) }
            }

            if ([System.IO.Path]::GetExtension($FilePath) -ne '.md') {
                [void] $errors.Add('Message file must use .md extension.')
            }

            $frontMatter = Read-MessageFrontMatter -FilePath $FilePath

            foreach ($field in $requiredFields) {
                if (-not $frontMatter.Contains($field)) {
                    [void] $errors.Add("Missing required field '$field'.")
                }
            }

            if ($frontMatter.Contains('from') -and $frontMatter['from'] -notin $validAgents) {
                [void] $errors.Add("Invalid from '$($frontMatter['from'])'.")
            }
            if ($frontMatter.Contains('to') -and $frontMatter['to'] -notin $validAgents) {
                [void] $errors.Add("Invalid to '$($frontMatter['to'])'.")
            }
            if ($frontMatter.Contains('type') -and $frontMatter['type'] -notin $validTypes) {
                [void] $errors.Add("Invalid type '$($frontMatter['type'])'.")
            }
            if ($frontMatter.Contains('status') -and $frontMatter['status'] -notin $validStatuses) {
                [void] $errors.Add("Invalid status '$($frontMatter['status'])'.")
            }
            if ($frontMatter.Contains('priority') -and $frontMatter['priority'] -notin $validPriorities) {
                [void] $errors.Add("Invalid priority '$($frontMatter['priority'])'.")
            }

            foreach ($boolField in @('requires_human', 'can_write')) {
                if ($frontMatter.Contains($boolField) -and -not (Test-BooleanString $frontMatter[$boolField])) {
                    [void] $errors.Add("Field '$boolField' must be true or false.")
                }
            }

            if ($frontMatter.Contains('created_at')) {
                $createdAt = [string]$frontMatter['created_at']
                if ($createdAt -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$') {
                    [void] $errors.Add("Field 'created_at' must be ISO 8601, e.g. 2026-05-27T11:45:00+08:00.")
                }
            }

            foreach ($listField in @('allowed_files', 'forbidden_files', 'context_files', 'acceptance')) {
                if ($frontMatter.Contains($listField)) {
                    $value = $frontMatter[$listField]
                    if ($value -is [System.Collections.IDictionary]) {
                        [void] $errors.Add("Field '$listField' must be a list, not a map.")
                    } elseif ($value -is [string] -and -not [string]::IsNullOrWhiteSpace($value)) {
                        [void] $errors.Add("Field '$listField' must be a YAML list.")
                    }
                }
            }

            $canWrite = $false
            if ($frontMatter.Contains('can_write') -and (Test-BooleanString $frontMatter['can_write'])) {
                $canWrite = ConvertTo-BoolStrict $frontMatter['can_write']
            }

            $allowedFiles = Ensure-List $frontMatter['allowed_files']
            $forbiddenFiles = Ensure-List $frontMatter['forbidden_files']

            if (-not $canWrite -and $allowedFiles.Count -gt 0) {
                [void] $errors.Add("Field 'allowed_files' must be empty when can_write is false; use context_files for read-only guidance.")
            }
            if ($canWrite -and $allowedFiles.Count -eq 0) {
                [void] $errors.Add("Field 'allowed_files' must contain at least one path when can_write is true.")
            }

            foreach ($allowed in $allowedFiles) {
                foreach ($forbidden in $forbiddenFiles) {
                    if ($allowed -eq $forbidden -or $allowed -like $forbidden -or $forbidden -like $allowed) {
                        [void] $errors.Add("Path '$allowed' overlaps forbidden path '$forbidden'.")
                    }
                }
            }

            if ($frontMatter.Contains('attempt')) {
                $tmp = 0
                if (-not [int]::TryParse([string]$frontMatter['attempt'], [ref]$tmp) -or $tmp -lt 0) {
                    [void] $errors.Add("Field 'attempt' must be a non-negative integer.")
                }
            }
            if ($frontMatter.Contains('max_attempts')) {
                $tmp = 0
                if (-not [int]::TryParse([string]$frontMatter['max_attempts'], [ref]$tmp) -or $tmp -lt 1) {
                    [void] $errors.Add("Field 'max_attempts' must be a positive integer.")
                }
            }
            if ($frontMatter.Contains('timeout_minutes')) {
                $tmp = 0
                if (-not [int]::TryParse([string]$frontMatter['timeout_minutes'], [ref]$tmp) -or $tmp -lt 1) {
                    [void] $errors.Add("Field 'timeout_minutes' must be a positive integer.")
                }
            }
        } catch {
            [void] $errors.Add($_.Exception.Message)
        }

        return [pscustomobject]@{
            Path = $FilePath
            Valid = ($errors.Count -eq 0)
            Errors = @($errors)
        }
    }

    $results = New-Object System.Collections.ArrayList
}

process {
    foreach ($item in $Path) {
        $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($item)
        [void] $results.Add((Test-MessageFile -FilePath $resolved))
    }
}

end {
    if ($results.Count -eq 0) {
        foreach ($item in $Path) {
            $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($item)
            [void] $results.Add((Test-MessageFile -FilePath $resolved))
        }
    }

    if ($Json) {
        $results | ConvertTo-Json -Depth 8
    } else {
        foreach ($result in $results) {
            if ($result.Valid) {
                Write-Output "OK  $($result.Path)"
            } else {
                Write-Output "ERR $($result.Path)"
                foreach ($err in $result.Errors) {
                    Write-Output "  - $err"
                }
            }
        }
    }

    if (@($results | Where-Object { -not $_.Valid }).Count -gt 0) {
        exit 1
    }
}
