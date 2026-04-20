param(
    [string]$ConfigPath = "$env:USERPROFILE\.codex\config.toml",
    [int]$StartupTimeoutSec = 60,
    [switch]$Login
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Section {
    param([string]$Message)
    Write-Host ""
    Write-Host $Message -ForegroundColor Cyan
}

function Set-OrReplace-TopLevelTomlAssignment {
    param(
        [string]$Content,
        [string]$Key,
        [string]$ValueLiteral
    )

    $pattern = "(?m)^" + [regex]::Escape($Key) + "\s*=.*$"
    $replacement = "$Key = $ValueLiteral"
    if ([regex]::IsMatch($Content, $pattern)) {
        return [regex]::Replace($Content, $pattern, $replacement, 1)
    }
    if ([string]::IsNullOrWhiteSpace($Content)) {
        return $replacement + [Environment]::NewLine
    }
    return ($replacement + [Environment]::NewLine + [Environment]::NewLine + $Content.TrimStart())
}

function Upsert-AtlassianMcpServerBlock {
    param(
        [string]$Content,
        [int]$TimeoutSec
    )

    $desiredBlock = @"
[mcp_servers.atlassian]
url = "https://mcp.atlassian.com/v1/mcp"
"@

    $blockPattern = "(?ms)^\[mcp_servers\.atlassian\]\r?\n.*?(?=^\[[^\]]+\]|\z)"
    $trimmedContent = $Content.Trim()
    if ([string]::IsNullOrWhiteSpace($trimmedContent)) {
        return $desiredBlock + [Environment]::NewLine
    }
    if ([regex]::IsMatch($Content, $blockPattern)) {
        $updated = [regex]::Replace($Content, $blockPattern, ($desiredBlock + [Environment]::NewLine), 1)
        return $updated.Trim() + [Environment]::NewLine
    }
    return ($trimmedContent + [Environment]::NewLine + [Environment]::NewLine + $desiredBlock + [Environment]::NewLine)
}

Write-Section "Checking npm and Codex CLI"
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "npm is not on PATH. Install Node.js (includes npm) and try again."
}

if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Codex CLI via npm..."
    npm install -g @openai/codex
} else {
    Write-Host "Codex CLI already installed."
}

Write-Section "Writing Codex config"
$configDir = Split-Path -Parent $ConfigPath
if (-not (Test-Path $configDir)) {
    New-Item -ItemType Directory -Path $configDir | Out-Null
}

$existingContent = ""
if (Test-Path $ConfigPath) {
    $existingContent = Get-Content -LiteralPath $ConfigPath -Raw
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $backupPath = "$ConfigPath.bak_$stamp"
    Copy-Item -LiteralPath $ConfigPath -Destination $backupPath -Force
    Write-Host "Backed up existing config to $backupPath"
}

$configContent = Set-OrReplace-TopLevelTomlAssignment `
    -Content $existingContent `
    -Key "mcp_oauth_credentials_store" `
    -ValueLiteral '"file"'
$configContent = Upsert-AtlassianMcpServerBlock `
    -Content $configContent `
    -TimeoutSec $StartupTimeoutSec

Set-Content -LiteralPath $ConfigPath -Value $configContent -Encoding UTF8
Write-Host "Wrote $ConfigPath"

Write-Section "MCP status"
codex mcp list
Write-Host ""
codex mcp get atlassian

if ($Login) {
    Write-Section "Logging in to Atlassian MCP"
    codex mcp login atlassian
    Write-Section "MCP status after login"
    codex mcp list
} else {
    Write-Host "Run 'codex mcp login atlassian' to complete OAuth."
}
