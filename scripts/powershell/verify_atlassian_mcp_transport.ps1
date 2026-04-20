param(
    [string]$CodexConfigPath = "$env:USERPROFILE\.codex\config.toml",
    [string]$WorkspaceMcpPath = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) ".vscode\mcp.json"),
    [switch]$ShowCodexList,
    [switch]$RunSmokeTest,
    [string]$SmokeTestPrompt = "Using Atlassian MCP, find Jira issue JVNAUTOSCI-1946 and return its key, summary, and status."
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$CurrentEndpoint = "https://mcp.atlassian.com/v1/mcp"
$DeprecatedEndpoint = "https://mcp.atlassian.com/v1/sse"

function Write-Section {
    param([string]$Message)
    Write-Host ""
    Write-Host $Message -ForegroundColor Cyan
}

function Get-TomlAtlassianEndpoint {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return @{
            Path = $Path
            Exists = $false
            Status = "missing_file"
            Endpoint = $null
            Note = "File not found."
        }
    }

    $content = Get-Content -LiteralPath $Path -Raw
    $blockPattern = "(?ms)^\[mcp_servers\.atlassian\]\r?\n.*?(?=^\[[^\]]+\]|\z)"
    $match = [regex]::Match($content, $blockPattern)
    if (-not $match.Success) {
        return @{
            Path = $Path
            Exists = $true
            Status = "missing_server"
            Endpoint = $null
            Note = "No [mcp_servers.atlassian] block found."
        }
    }

    $block = $match.Value
    $endpointMatch = [regex]::Match($block, "https://mcp\.atlassian\.com/v1/(?:mcp|sse)")
    if (-not $endpointMatch.Success) {
        return @{
            Path = $Path
            Exists = $true
            Status = "unknown"
            Endpoint = $null
            Note = "Atlassian block found, but no recognised endpoint string was detected."
        }
    }

    $endpoint = $endpointMatch.Value
    $hasDirectUrl = [regex]::IsMatch($block, "(?m)^\s*url\s*=")
    $hasLegacyCommandWrapper = [regex]::IsMatch($block, "(?m)^\s*command\s*=") -or [regex]::IsMatch($block, "(?m)^\s*args\s*=")
    $status = if ($endpoint -eq $DeprecatedEndpoint) {
        "stale"
    } elseif ($endpoint -eq $CurrentEndpoint -and $hasDirectUrl -and -not $hasLegacyCommandWrapper) {
        "current"
    } elseif ($endpoint -eq $CurrentEndpoint) {
        "legacy_wrapper"
    } else {
        "unknown"
    }

    $note = switch ($status) {
        "current" { "Detected native Codex streamable-HTTP Atlassian MCP endpoint." }
        "legacy_wrapper" { "Detected Atlassian endpoint inside a legacy stdio wrapper block. Codex native OAuth/login expects a direct url server entry." }
        default { "Detected Atlassian Codex CLI MCP endpoint." }
    }

    return @{
        Path = $Path
        Exists = $true
        Status = $status
        Endpoint = $endpoint
        Note = $note
    }
}

function Get-JsonAtlassianEndpoint {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return @{
            Path = $Path
            Exists = $false
            Status = "missing_file"
            Endpoint = $null
            Note = "File not found."
        }
    }

    $raw = Get-Content -LiteralPath $Path -Raw
    $json = $raw | ConvertFrom-Json
    $servers = $null
    if ($null -ne $json.servers) {
        $servers = $json.servers
    } elseif ($null -ne $json.mcpServers) {
        $servers = $json.mcpServers
    }
    if ($null -eq $servers -or $null -eq $servers.atlassian) {
        return @{
            Path = $Path
            Exists = $true
            Status = "missing_server"
            Endpoint = $null
            Note = "No Atlassian server entry found."
        }
    }

    $endpoint = [string]$servers.atlassian.url
    if ([string]::IsNullOrWhiteSpace($endpoint)) {
        return @{
            Path = $Path
            Exists = $true
            Status = "unknown"
            Endpoint = $null
            Note = "Atlassian entry found, but it has no url."
        }
    }

    $status = if ($endpoint -eq $CurrentEndpoint) {
        "current"
    } elseif ($endpoint -eq $DeprecatedEndpoint) {
        "stale"
    } else {
        "unknown"
    }

    return @{
        Path = $Path
        Exists = $true
        Status = $status
        Endpoint = $endpoint
        Note = "Detected Atlassian MCP workspace/user endpoint."
    }
}

function Write-Report {
    param(
        [string]$Label,
        [hashtable]$Report
    )

    $statusColour = switch ($Report.Status) {
        "current" { "Green" }
        "stale" { "Yellow" }
        "legacy_wrapper" { "Yellow" }
        "missing_server" { "DarkYellow" }
        "missing_file" { "DarkGray" }
        default { "Magenta" }
    }

    Write-Host "$Label" -ForegroundColor White
    Write-Host ("  path:     {0}" -f $Report.Path)
    Write-Host ("  status:   {0}" -f $Report.Status) -ForegroundColor $statusColour
    if ($Report.Endpoint) {
        Write-Host ("  endpoint: {0}" -f $Report.Endpoint)
    }
    if ($Report.Note) {
        Write-Host ("  note:     {0}" -f $Report.Note)
    }
}

$userMcpPath = Join-Path $env:APPDATA "Code\User\mcp.json"
$insidersMcpPath = Join-Path $env:APPDATA "Code - Insiders\User\mcp.json"

Write-Section "Atlassian MCP transport inventory"
Write-Host ("Canonical endpoint: {0}" -f $CurrentEndpoint)

$reports = @(
    @{ Label = "Codex CLI config"; Report = Get-TomlAtlassianEndpoint -Path $CodexConfigPath },
    @{ Label = "Workspace VS Code MCP config"; Report = Get-JsonAtlassianEndpoint -Path $WorkspaceMcpPath },
    @{ Label = "VS Code user MCP config"; Report = Get-JsonAtlassianEndpoint -Path $userMcpPath },
    @{ Label = "VS Code Insiders user MCP config"; Report = Get-JsonAtlassianEndpoint -Path $insidersMcpPath }
)

foreach ($item in $reports) {
    Write-Report -Label $item.Label -Report $item.Report
}

if ($ShowCodexList) {
    Write-Section "codex mcp list"
    if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
        Write-Host "codex command not found on PATH." -ForegroundColor Yellow
    } else {
        codex mcp list
        Write-Host ""
        Write-Host "codex mcp get atlassian"
        codex mcp get atlassian
    }
}

if ($RunSmokeTest) {
    Write-Section "Codex Atlassian MCP smoke test"
    if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
        throw "codex command not found on PATH."
    }
    codex exec -s read-only $SmokeTestPrompt
}
