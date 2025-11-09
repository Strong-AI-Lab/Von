<#
.SYNOPSIS
    Setup script for installing/updating JavaScript (Node) dependencies.

.DESCRIPTION
    Ensures Node.js & npm exist, installs dependencies from package.json.
    Skips install if node_modules present unless -Force used.
    Outputs jest version after successful install for verification.

.PARAMETER Force
    Force reinstall (always run `npm install`).

.PARAMETER CI
    Reduce output noise for CI environments (adds --no-audit --no-fund, minimal logs).

.PARAMETER NoJestCheck
    Skip printing jest version after install.

.EXAMPLES
    ./setup_js.ps1
    ./setup_js.ps1 -Force
    ./setup_js.ps1 -CI
    ./setup_js.ps1 -Force -CI
#>
param(
    [switch]$Force,
    [switch]$CI,
    [switch]$NoJestCheck
)
$ErrorActionPreference = 'Stop'

if (-not (Test-Path 'package.json')) {
    Write-Error 'package.json not found in current directory; run from project root.'
    exit 1
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Write-Error 'Node.js is required but was not found. Install from https://nodejs.org/' ; exit 1 }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Write-Error 'npm is required but was not found (it ships with Node.js).' ; exit 1 }

$nodeVersion = (node --version) 2>$null
$npmVersion  = (npm --version) 2>$null
Write-Host ("Node.js {0} / npm {1}" -f $nodeVersion,$npmVersion)

$skip = $false
if (-not $Force -and (Test-Path 'node_modules')) {
    Write-Host 'node_modules already present; skipping install (use -Force to override).'
    $skip = $true
}

if (-not $skip) {
    Write-Host 'Installing JavaScript dependencies with npm...'
    $args = @('install')
    if ($CI) { $args += @('--no-fund','--no-audit') }
    npm @args
    Write-Host 'npm install complete.'
} else {
    Write-Host 'Skipped install.'
}

if (-not $NoJestCheck) {
    try {
        $jestVersion = (npx jest --version) 2>$null
        if ($jestVersion) { Write-Host ("jest version: {0}" -f $jestVersion) }
        else { Write-Host 'jest not found after install (unexpected)'; exit 2 }
    } catch {
        Write-Host 'jest invocation failed.'
        exit 2
    }
}

Write-Host '=== JavaScript setup done ==='

