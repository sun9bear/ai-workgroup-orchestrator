param(
    [string] $WorkgroupRoot = 'docs/ai-workgroup'
)

$ErrorActionPreference = 'Stop'

$stateDir = Join-Path $WorkgroupRoot 'state'
$outputPath = Join-Path $stateDir 'events.jsonl'
$agentLogs = Get-ChildItem -LiteralPath $stateDir -Filter 'events.*.jsonl' -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -ne 'events.jsonl' }

$events = New-Object System.Collections.ArrayList

function Read-LinesWithRetry {
    param(
        [string] $Path,
        [int] $Attempts = 10
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            return [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)
        } catch {
            if ($attempt -eq $Attempts) {
                throw
            }
            Start-Sleep -Milliseconds (50 * $attempt)
        }
    }
}

foreach ($log in $agentLogs) {
    foreach ($line in (Read-LinesWithRetry -Path $log.FullName)) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        try {
            $event = $line | ConvertFrom-Json
            $event | Add-Member -NotePropertyName '_source' -NotePropertyValue $log.Name -Force
            [void] $events.Add($event)
        } catch {
            $bad = [pscustomobject]@{
                type = 'event_parse_failed'
                at = '9999-12-31T23:59:59+00:00'
                agent = ''
                message_id = ''
                path = $log.FullName
                error = $_.Exception.Message
                _source = $log.Name
            }
            [void] $events.Add($bad)
        }
    }
}

$sorted = $events | Sort-Object @{ Expression = { if ($_.at) { $_.at } else { '' } } }, @{ Expression = { if ($_.agent) { $_.agent } else { '' } } }
$lines = foreach ($event in $sorted) {
    $event | ConvertTo-Json -Compress
}

[System.IO.File]::WriteAllLines($outputPath, [string[]]$lines, [System.Text.UTF8Encoding]::new($false))
Write-Output "Aggregated $($events.Count) events into $outputPath"
