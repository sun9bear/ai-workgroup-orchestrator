param(
    [string] $TargetProjectRoot = 'D:\example\protected-business-repo',
    [string] $PlanPath = 'D:\example\protected-business-repo\docs\plans\2026-06-01-anonymous-preview-funnel-ux-plan.md',
    [string] $WorkgroupRelativePath = 'docs/ai-workgroup',
    [switch] $SkipClaudeReviewTask,
    [switch] $Json
)

$ErrorActionPreference = 'Stop'

$initScript = Join-Path $PSScriptRoot 'Initialize-WorkgroupProject.ps1'
$messageScript = Join-Path $PSScriptRoot 'New-WorkgroupMessage.ps1'

if (-not (Test-Path -LiteralPath $initScript -PathType Leaf)) {
    throw "Missing script: $initScript"
}
if (-not (Test-Path -LiteralPath $messageScript -PathType Leaf)) {
    throw "Missing script: $messageScript"
}
if (-not (Test-Path -LiteralPath $TargetProjectRoot -PathType Container)) {
    throw "Target project root was not found: $TargetProjectRoot"
}
if (-not (Test-Path -LiteralPath $PlanPath -PathType Leaf)) {
    throw "Plan file was not found: $PlanPath"
}

$resolvedProjectRoot = (Resolve-Path -LiteralPath $TargetProjectRoot).ProviderPath
$resolvedPlanPath = (Resolve-Path -LiteralPath $PlanPath).ProviderPath
$workgroupRoot = Join-Path $resolvedProjectRoot $WorkgroupRelativePath

& $initScript -ProjectRoot $resolvedProjectRoot -WorkgroupRelativePath $WorkgroupRelativePath -Agents @('CodeX', 'Claude-Code', 'OpenCode', 'Human') | Out-Null

$relativePlanPath = $resolvedPlanPath
if ($resolvedPlanPath.StartsWith($resolvedProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    $relativePlanPath = $resolvedPlanPath.Substring($resolvedProjectRoot.Length).TrimStart('\') -replace '\\', '/'
}

$codexBody = @"
## Request

You are a new CodeX session taking over the anonymous preview funnel UX plan.

Do not write implementation code yet. Perform a read-only intake audit and produce a task breakdown for Claude Code.

Plan:

```text
$relativePlanPath
```

Target project:

```text
$resolvedProjectRoot
```

## Required Read-Only Steps

1. Read the plan document completely enough to understand the phase order.
2. Check current repository state:

```powershell
git -C "$resolvedProjectRoot" status --short
git -C "$resolvedProjectRoot" branch --show-current
```

3. Inspect relevant project areas:

```text
frontend-next/src
frontend-next/public
gateway
src/services/content_compliance.py
gateway/entitlements.py
gateway/pricing_schema.py
gateway/credits_service.py
gateway/voice_selection_api.py
gateway/user_voice_service.py
gateway/job_intercept.py
```

4. Search for already implemented or partial related work:

```powershell
rg -n "anonymous|preview|claim token|watermark|sample|hero|立即试用|youtube_url|smart_preview|studio_visible|anonymous_preview" "$resolvedProjectRoot" -g "!*.log" -g "!.git/**" -g "!.next/**" -g "!.venv/**" -g "!node_modules/**"
```

## Output

Write an intake report to `docs/ai-workgroup/inbox/Human/` in this target project.

The report must include:

- plan goal summary;
- current repository state;
- implemented or likely implemented parts;
- missing parts;
- risks and Human Gate decisions;
- recommended implementation order;
- Phase 1 Claude Code task cards;
- each Claude task card must include `allowed_files`, `forbidden_files`, `acceptance`, and test commands.

## Constraints

- Do not implement code in this intake task.
- Do not revert or clean existing git changes.
- Do not open automatic write runners.
- Keep Phase 1 focused on the homepage/sample-player/CTA shell unless the audit proves a different low-risk starting point.
- Business implementation code should be assigned to Claude Code after Human approval.
"@

$codexTask = & $messageScript `
    -ProjectRoot $resolvedProjectRoot `
    -WorkgroupRelativePath $WorkgroupRelativePath `
    -Task 'APF0-codex-intake-audit' `
    -From 'Human' `
    -To 'CodeX' `
    -Type 'instruction' `
    -Priority 'high' `
    -RequiresHuman:$false `
    -CanWrite:$false `
    -ContextFiles @($relativePlanPath, 'CLAUDE.md', 'AGENTS.md') `
    -Title 'APF0 CodeX Intake Audit For Anonymous Preview Funnel' `
    -Body $codexBody

$createdMessages = New-Object System.Collections.ArrayList
[void] $createdMessages.Add($codexTask)

if (-not $SkipClaudeReviewTask) {
    $claudeBody = @"
## Request

You are Claude Code. Perform a read-only review for the anonymous preview funnel plan.

Do not write code. Do not modify files.

Plan:

```text
$relativePlanPath
```

Target project:

```text
$resolvedProjectRoot
```

## Review Questions

1. Where is the current frontend homepage or marketing entry?
2. Which files should Phase 1 modify for the homepage sample player and CTA shell?
3. Is Phase 1 implementable without touching Gateway?
4. Are there existing demo/sample/video/hero components or assets that should be reused?
5. What risks could accidentally trigger backend preview, clone, payment, or entitlement paths?
6. Propose the first implementation task card for Claude Code, including `allowed_files`, `forbidden_files`, `acceptance`, and test commands.

## Output

Write a read-only review report to `docs/ai-workgroup/inbox/CodeX/`.

## Constraints

- can_write is false.
- Do not implement code.
- Do not change frontend, gateway, tests, or docs except writing the requested report.
- If the task is unclear, write `needs_clarification` in the report rather than guessing.
"@

    $claudeTask = & $messageScript `
        -ProjectRoot $resolvedProjectRoot `
        -WorkgroupRelativePath $WorkgroupRelativePath `
        -Task 'APF0-claude-readonly-boundary-review' `
        -From 'CodeX' `
        -To 'Claude-Code' `
        -Type 'instruction' `
        -Priority 'medium' `
        -RequiresHuman:$false `
        -CanWrite:$false `
        -ContextFiles @($relativePlanPath, 'frontend-next/src', 'frontend-next/public', 'gateway') `
        -Title 'APF0 Claude Read-Only Boundary Review' `
        -Body $claudeBody

    [void] $createdMessages.Add($claudeTask)
}

$nextActionPath = Join-Path $workgroupRoot 'NEXT_ACTION.md'
$messageList = @($createdMessages | ForEach-Object { "- $($_.to): $($_.path)" }) -join "`n"
$nextAction = @"
# AI Workgroup Next Action

Generated at: $((Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK"))

## Created Messages

$messageList

## Human Trigger Commands

For the new CodeX session, send:

Read the latest task in docs/ai-workgroup/inbox/CodeX and execute it read-only. Do not write implementation code.

For Claude Code, after CodeX confirms the boundary or when Human wants the parallel read-only review, send:

Read the latest task in docs/ai-workgroup/inbox/Claude-Code and write the requested report to docs/ai-workgroup/inbox/CodeX. Do not implement code.

## Current Rule

Business implementation code is written by Claude Code only after CodeX produces task cards and Human approves one task.
"@
[System.IO.File]::WriteAllText($nextActionPath, $nextAction, [System.Text.UTF8Encoding]::new($false))

$result = [pscustomobject]@{
    project_root = $resolvedProjectRoot
    workgroup_root = $workgroupRoot
    plan_path = $resolvedPlanPath
    messages = @($createdMessages)
    next_action = $nextActionPath
}

if ($Json) {
    $result | ConvertTo-Json -Depth 8
} else {
    $result
}
