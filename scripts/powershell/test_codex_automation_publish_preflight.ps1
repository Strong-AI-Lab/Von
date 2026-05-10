<#
.SYNOPSIS
    Verify that Codex automation can publish through the GitHub REST API path.

.DESCRIPTION
    This preflight is intended for unattended Jira automation runs. It loads
    VON_CODEX_AUTOMATION_TOKEN from the repo-root .env into GH_TOKEN for this
    PowerShell process only, avoids gh auth status output, verifies basic GitHub
    identity/repository access, and proves ref mutation by creating, reading,
    and deleting a temporary branch ref.

    The script never prints the token and does not use git push.

.PARAMETER RepoRoot
    Repository root. Defaults to the root containing this script.

.PARAMETER OwnerRepo
    GitHub owner/repository name.

.PARAMETER TokenVariableName
    Repo .env variable to load into GH_TOKEN.

.PARAMETER TemporaryRef
    Temporary Git ref to create/read/delete. Must be under refs/heads/.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\test_codex_automation_publish_preflight.ps1
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$RepoRoot,

    [Parameter(Mandatory = $false)]
    [string]$OwnerRepo = "Strong-AI-Lab/Von",

    [Parameter(Mandatory = $false)]
    [string]$TokenVariableName = "VON_CODEX_AUTOMATION_TOKEN",

    [Parameter(Mandatory = $false)]
    [string]$TemporaryRef = "refs/heads/codex/api-preflight-token-test"
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[codex-publish-preflight] $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "[codex-publish-preflight] $Message" -ForegroundColor Green
}

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Repo .env file not found at $Path."
    }

    $escapedName = [regex]::Escape($Name)
    $line = Get-Content -LiteralPath $Path |
        Where-Object { $_ -match "^\s*$escapedName\s*=" } |
        Select-Object -Last 1

    if (-not $line) {
        throw "$Name was not found in repo .env."
    }

    $value = ($line -replace "^\s*$escapedName\s*=\s*", "").Trim()
    if (
        ($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))
    ) {
        $value = $value.Substring(1, $value.Length - 2)
    }

    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "$Name was empty in repo .env."
    }

    return $value
}

function Invoke-Gh {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $false)]
        [string]$TokenForRedaction
    )

    $output = & gh @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    $text = ($output | Out-String).Trim()
    if ($TokenForRedaction) {
        $text = $text.Replace($TokenForRedaction, "<redacted>")
    }
    if ($exitCode -ne 0) {
        throw "gh $($Arguments -join ' ') failed with exit code ${exitCode}: $text"
    }
    return $text
}

function Invoke-GhForExitCode {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $false)]
        [string]$TokenForRedaction
    )

    $oldErrorActionPreference = $ErrorActionPreference
    try {
        $script:ErrorActionPreference = "Continue"
        $output = & gh @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $script:ErrorActionPreference = $oldErrorActionPreference
    }

    $text = ($output | Out-String).Trim()
    if ($TokenForRedaction) {
        $text = $text.Replace($TokenForRedaction, "<redacted>")
    }

    return @{
        ExitCode = $exitCode
        Output = $text
    }
}

function Test-GhNotFound {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $result = Invoke-GhForExitCode -Arguments $Arguments
    return ($result.ExitCode -ne 0)
}

if (-not $RepoRoot) {
    $RepoRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")
}
else {
    $RepoRoot = Resolve-Path -LiteralPath $RepoRoot
}

if (-not $TemporaryRef.StartsWith("refs/heads/")) {
    throw "TemporaryRef must be under refs/heads/."
}

$envPath = Join-Path $RepoRoot ".env"
$token = Get-DotEnvValue -Path $envPath -Name $TokenVariableName
$oldGhToken = $env:GH_TOKEN
$tempBodyPath = $null
$refPath = $TemporaryRef.Substring("refs/".Length)
$getTempRefEndpoint = "repos/$OwnerRepo/git/ref/$refPath"
$deleteTempRefEndpoint = "repos/$OwnerRepo/git/refs/$refPath"

try {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw "GitHub CLI 'gh' is not available on PATH."
    }

    $env:GH_TOKEN = $token

    Write-Step "Verifying GitHub identity and repository access"
    $user = Invoke-Gh -Arguments @("api", "user", "--jq", "{login: .login, id: .id}") -TokenForRedaction $token
    $repo = Invoke-Gh -Arguments @("repo", "view", $OwnerRepo, "--json", "nameWithOwner,viewerPermission") -TokenForRedaction $token
    $mainSha = Invoke-Gh -Arguments @("api", "repos/$OwnerRepo/git/ref/heads/main", "--jq", ".object.sha") -TokenForRedaction $token

    Write-Step "Creating and deleting temporary ref $TemporaryRef"
    Invoke-GhForExitCode -Arguments @("api", "-X", "DELETE", $deleteTempRefEndpoint) -TokenForRedaction $token | Out-Null

    $tempBodyPath = [System.IO.Path]::GetTempFileName()
    $bodyJson = @{
        ref = $TemporaryRef
        sha = $mainSha
    } | ConvertTo-Json -Compress
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($tempBodyPath, $bodyJson, $utf8NoBom)

    Invoke-Gh -Arguments @("api", "-X", "POST", "repos/$OwnerRepo/git/refs", "--input", $tempBodyPath) -TokenForRedaction $token | Out-Null
    $readSha = Invoke-Gh -Arguments @("api", $getTempRefEndpoint, "--jq", ".object.sha") -TokenForRedaction $token
    if ($readSha -ne $mainSha) {
        throw "Temporary ref SHA did not match main SHA."
    }

    Invoke-Gh -Arguments @("api", "-X", "DELETE", $deleteTempRefEndpoint) -TokenForRedaction $token | Out-Null
    if (-not (Test-GhNotFound -Arguments @("api", $getTempRefEndpoint))) {
        throw "Temporary ref still exists after delete."
    }

    Write-Output "USER=$user"
    Write-Output "REPO=$repo"
    Write-Output "MAIN_SHA=$mainSha"
    Write-Output "TEMP_REF_FINAL=not-found"
    Write-Ok "Publish preflight passed"
}
finally {
    if ($tempBodyPath -and (Test-Path -LiteralPath $tempBodyPath)) {
        Remove-Item -LiteralPath $tempBodyPath -Force
    }
    if ($null -eq $oldGhToken) {
        Remove-Item Env:\GH_TOKEN -ErrorAction SilentlyContinue
    }
    else {
        $env:GH_TOKEN = $oldGhToken
    }
}
