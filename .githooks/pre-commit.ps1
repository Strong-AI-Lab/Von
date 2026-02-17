#!/usr/bin/env pwsh
# Blocks staged runtime data from data/rag_storage, data/raw, or logs.
$blockedPatterns = @(
    '^data/rag_storage/',
    '^data/raw/',
    '^data/.+\.json$',
    '^logs/'
)

$staged = git diff --cached --name-only
if (-not $staged) {
    exit 0
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

exit 0
