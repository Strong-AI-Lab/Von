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

$configContent = @"
# Codex CLI configuration

mcp_oauth_credentials_store = "file"

[mcp_servers.atlassian]
command = "npx"
args = ["-y", "mcp-remote", "https://mcp.atlassian.com/v1/mcp"]
startup_timeout_sec = $StartupTimeoutSec
"@

Set-Content -Path $ConfigPath -Value $configContent -Encoding UTF8
Write-Host "Wrote $ConfigPath"

Write-Section "MCP status"
codex mcp list

if ($Login) {
    Write-Section "Logging in to Atlassian MCP"
    codex mcp login atlassian
    Write-Section "MCP status after login"
    codex mcp list
} else {
    Write-Host "Run 'codex mcp login atlassian' to complete OAuth."
}
