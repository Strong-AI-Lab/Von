#!/usr/bin/env pwsh
# Blocks staged runtime data from data/rag_storage, data/raw, or logs.
$blockedPatterns = @(
    '^data/rag_storage/',
    '^data/raw/',
    '^data/.+\.json$',
    '^backups/',
    '^logs/'
)

$staged = git diff --cached --name-only
if (-not $staged) {
    exit 0
}

$repoRoot = (git rev-parse --show-toplevel).Trim()

function Resolve-RepoPython {
    param([string]$RepoRoot)

    $candidates = @(
        (Join-Path $RepoRoot '.venv\Scripts\python.exe'),
        (Join-Path $RepoRoot '.venv/bin/python')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    return 'python'
}

$hits = @()
foreach ($file in $staged) {
    foreach ($pattern in $blockedPatterns) {
        if ($file -match $pattern) {
            $hits += $file
            break
        }
    }
}

if ($hits.Count -gt 0) {
    Write-Host "Blocked committing runtime data files:" -ForegroundColor Red
    $hits | Sort-Object -Unique | ForEach-Object { Write-Host " - $_" }
    Write-Host "Remove from index or move out of blocked paths, then commit again." -ForegroundColor Yellow
    exit 1
}

# JVNAUTOSCI-1171: prevent new runtime usage of deprecated preserved_fields.
# This blocks newly-added references unless they are in explicit migration/guard/test paths.
$legacyPattern = 'concept_data\.preserved_fields'
$legacyAllowPatterns = @(
    '^src/backend/services/preserved_fields_decommission_service\.py$',
    '^src/backend/utilities/migrate_preserved_fields_to_text_relations\.py$',
    '^src/backend/services/concept_service\.py$',
    '^src/backend/server/routes/settings_routes\.py$',
    '^tests/'
)

$legacyHits = @()
foreach ($file in $staged) {
    if ($file -notmatch '\.(py|md|txt|json|yaml|yml|js|ts)$') {
        continue
    }

    $isAllowed = $false
    foreach ($allowPattern in $legacyAllowPatterns) {
        if ($file -match $allowPattern) {
            $isAllowed = $true
            break
        }
    }
    if ($isAllowed) {
        continue
    }

    $diffLines = git diff --cached --unified=0 -- $file
    $addedLegacyLine = $diffLines | Where-Object {
        $_ -match '^\+' -and $_ -notmatch '^\+\+\+' -and $_ -match $legacyPattern
    } | Select-Object -First 1

    if ($addedLegacyLine) {
        $legacyHits += $file
    }
}

if ($legacyHits.Count -gt 0) {
    Write-Host "Blocked committing new deprecated field references:" -ForegroundColor Red
    $legacyHits | Sort-Object -Unique | ForEach-Object { Write-Host " - $_" }
    Write-Host "Use canonical text relations (hasDescription/hasNote/hasContent) or approved migration helpers." -ForegroundColor Yellow
    exit 1
}

$pythonSyntaxPaths = @(
    $staged | Where-Object { $_ -match '\.py$' }
)

if ($pythonSyntaxPaths.Count -gt 0) {
    $compatScript = Join-Path $repoRoot 'scripts/check_python_min_syntax.py'
    if (-not (Test-Path $compatScript)) {
        Write-Host "Python syntax compatibility script is missing: $compatScript" -ForegroundColor Red
        exit 1
    }

    $python = Resolve-RepoPython -RepoRoot $repoRoot
    Write-Host "Running Python minimum syntax compatibility gate..." -ForegroundColor Cyan
    Push-Location $repoRoot
    try {
        & $python $compatScript --paths @pythonSyntaxPaths
        $commandSucceeded = $?
        if (-not $commandSucceeded -or $LASTEXITCODE -ne 0) {
            Write-Host "Python syntax compatibility gate failed. Keep repo Python parseable by the minimum supported Python version." -ForegroundColor Red
            exit 1
        }
    }
    finally {
        Pop-Location
    }
}

$workflowPurityTriggerPatterns = @(
    '^src/backend/integrations/internal_mcp/orchestrator\.py$',
    '^src/backend/server/routes/von_routes\.py$',
    '^src/backend/workflows/',
    '^src/backend/services/.+workflow.+\.py$',
    '^src/backend/services/conversation_turn_workflow_vontology_service\.py$',
    '^scripts/check_workflow_purity\.py$',
    '^scripts/workflow_purity_report\.py$',
    '^tests/backend/test_workflow_purity.*\.py$',
    '^tests/backend/fixtures/workflow_purity_baseline\.json$',
    '^\.githooks/pre-commit$',
    '^\.githooks/pre-commit\.ps1$'
)

$runWorkflowPurityGate = $false
foreach ($file in $staged) {
    foreach ($pattern in $workflowPurityTriggerPatterns) {
        if ($file -match $pattern) {
            $runWorkflowPurityGate = $true
            break
        }
    }
    if ($runWorkflowPurityGate) {
        break
    }
}

if ($runWorkflowPurityGate) {
    $purityScript = Join-Path $repoRoot 'scripts/check_workflow_purity.py'

    if (-not (Test-Path $purityScript)) {
        Write-Host "Workflow purity gate script is missing: $purityScript" -ForegroundColor Red
        exit 1
    }

    Write-Host "Running workflow purity gate..." -ForegroundColor Cyan
    $python = Resolve-RepoPython -RepoRoot $repoRoot
    Push-Location $repoRoot
    try {
        & $python $purityScript
        $commandSucceeded = $?
        if (-not $commandSucceeded -or $LASTEXITCODE -ne 0) {
            Write-Host "Workflow purity gate failed. Fix the reported violations or refresh the baseline deliberately." -ForegroundColor Red
            exit 1
        }
    }
    finally {
        Pop-Location
    }
}

exit 0
