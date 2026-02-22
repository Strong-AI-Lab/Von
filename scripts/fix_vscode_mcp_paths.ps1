<#
.SYNOPSIS
    Fix VS Code MCP server command paths for this workspace.

.DESCRIPTION
    Updates .vscode/mcp.json to use the local repo's virtual environment for
    Von MCP servers (vontology/vonrag) and a discovered uv.exe for arxiv.

    This is intended to resolve errors like:
      - "pdm.exe needed to run vontology/vonrag was not found"
      - "uv.exe needed to run arxiv was not found"

.PARAMETER RepoRoot
    Optional repo root path. Defaults to the parent directory of this script.

.PARAMETER DryRun
    Print the proposed changes without writing the file.

.EXAMPLES
    ./scripts/fix_vscode_mcp_paths.ps1
    ./scripts/fix_vscode_mcp_paths.ps1 -DryRun
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$RepoRoot,

    [Parameter(Mandatory = $false)]
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Assert-FileExists {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "File not found: $Path"
    }
}

function Get-RepoRoot {
    if (-not [string]::IsNullOrWhiteSpace($RepoRoot)) {
        return (Resolve-Path -LiteralPath $RepoRoot).Path
    }

    # scripts/ is directly under repo root
    return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
}

function Find-UvExe {
    $uvCmd = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($uvCmd -and $uvCmd.Source) {
        return $uvCmd.Source
    }

    $fallback = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path -LiteralPath $fallback) {
        return $fallback
    }

    return $null
}

$root = Get-RepoRoot
$mcpPath = Join-Path $root '.vscode\mcp.json'
Assert-FileExists $mcpPath

$pythonVenv = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonVenv)) {
    Write-Warning "Expected venv Python not found at: $pythonVenv"
    Write-Warning "Run ./setup_py.ps1 -ConfigureVSCode (or ./setup_all.ps1) first, then re-run this script."
}

$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd) {
    Write-Warning "uv was not found on PATH. arxiv MCP server may not start until uv is installed."
    Write-Warning "Run ./setup_py.ps1 (it installs uv) or install uv from https://docs.astral.sh/uv/."
}

# Read + parse JSON (file is jsonc but we keep it strict JSON output)
$raw = Get-Content -Raw -LiteralPath $mcpPath
$cfg = $raw | ConvertFrom-Json
if (-not $cfg.servers) {
    throw "Invalid mcp.json: missing 'servers'"
}

function Ensure-Server {
    param(
        $Servers,
        [string]$Name
    )

    if (-not $Servers.PSObject.Properties.Name -contains $Name) {
        $Servers | Add-Member -NotePropertyName $Name -NotePropertyValue (@{})
    }

    return $Servers.$Name
}

$servers = $cfg.servers
$vontology = Ensure-Server -Servers $servers -Name 'vontology'
$vonrag = Ensure-Server -Servers $servers -Name 'vonrag'
$arxiv = Ensure-Server -Servers $servers -Name 'arxiv'

# Use venv python directly to avoid pdm wrapper hangs in stdio MCP startup.
$vontology.type = 'stdio'
$vontology.command = '${workspaceFolder}\.venv\Scripts\python.exe'
$vontology.args = @('src/backend/mcp_server/mcp_stdio_server.py')
$vontology.cwd = '${workspaceFolder}'

$vonrag.type = 'stdio'
$vonrag.command = '${workspaceFolder}\.venv\Scripts\python.exe'
$vonrag.args = @('src/backend/mcp_server/rag_mcp_stdio_server.py')
$vonrag.cwd = '${workspaceFolder}'

# arxiv via uv tool run arxiv-mcp-server
$arxiv.type = 'stdio'
$arxiv.command = 'uv'
$arxiv.cwd = '${workspaceFolder}'

# Ensure storage path points inside this repo
$storagePath = Join-Path $root 'data\arxiv_papers'
if (-not (Test-Path -LiteralPath $storagePath)) {
    New-Item -ItemType Directory -Path $storagePath | Out-Null
}

$arxivArgs = @('tool', 'run', 'arxiv-mcp-server', '--storage-path', '${workspaceFolder}\\data\\arxiv_papers')
$arxiv.args = $arxivArgs

if ($DryRun) {
    Write-Host "--- Proposed .vscode/mcp.json (not written) ---" -ForegroundColor Cyan
    $cfg | ConvertTo-Json -Depth 20
    exit 0
}

# Write updated config
$cfg | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $mcpPath -Encoding UTF8
Write-Host "Updated: $mcpPath" -ForegroundColor Green
Write-Host "Restart VS Code MCP servers after this change." -ForegroundColor Green
