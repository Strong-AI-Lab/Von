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

exit 0
