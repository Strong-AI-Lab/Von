[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Host,

    [Parameter(Mandatory = $true)]
    [string]$User,

    [Parameter(Mandatory = $true)]
    [string]$ArtifactPath,

    [int]$Port = 22,
    [string]$SshKeyPath,
    [string]$KnownHostsPath,
    [switch]$SkipHostKeyChecking,
    [string]$RemoteArtifactDirectory = '/tmp',
    [string]$RemoteDeployScript = '/usr/local/bin/deploy_von_release.sh',
    [string]$RepoRef = 'main',
    [string]$RemoteAuditLogPath = '/var/log/von/deploy_audit.jsonl',
    [string]$AuditOutputPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-NormalisedPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function ConvertTo-ShellLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)
    $escaped = $Value -replace "'", "'\"'\"'"
    return "'$escaped'"
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$CaptureOutput
    )

    Write-Host ("{0} {1}" -f $FilePath, ($Arguments -join ' ')) -ForegroundColor Cyan

    if ($CaptureOutput) {
        $output = & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$FilePath failed with exit code $LASTEXITCODE."
        }
        return ($output -join [Environment]::NewLine)
    }

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE."
    }
}

if (-not (Test-Path -LiteralPath $ArtifactPath)) {
    throw "ArtifactPath '$ArtifactPath' does not exist."
}

$resolvedArtifactPath = Resolve-NormalisedPath -Path $ArtifactPath
$artifactName = [System.IO.Path]::GetFileName($resolvedArtifactPath)
$remoteArtifactPath = ($RemoteArtifactDirectory.TrimEnd('/') + '/' + $artifactName)
$artifactSha256 = (Get-FileHash -LiteralPath $resolvedArtifactPath -Algorithm SHA256).Hash.ToLowerInvariant()

$sshCommonArgs = @(
    '-o', 'BatchMode=yes',
    '-o', 'ConnectTimeout=20',
    '-p', "$Port"
)
$scpCommonArgs = @(
    '-o', 'BatchMode=yes',
    '-o', 'ConnectTimeout=20',
    '-P', "$Port"
)

if ($SshKeyPath) {
    $resolvedKeyPath = Resolve-NormalisedPath -Path $SshKeyPath
    $sshCommonArgs += @('-i', $resolvedKeyPath)
    $scpCommonArgs += @('-i', $resolvedKeyPath)
}

if ($KnownHostsPath) {
    $resolvedKnownHostsPath = Resolve-NormalisedPath -Path $KnownHostsPath
    $sshCommonArgs += @('-o', 'StrictHostKeyChecking=yes', '-o', "UserKnownHostsFile=$resolvedKnownHostsPath")
    $scpCommonArgs += @('-o', 'StrictHostKeyChecking=yes', '-o', "UserKnownHostsFile=$resolvedKnownHostsPath")
}
elseif ($SkipHostKeyChecking) {
    $nullDevice = if ($IsWindows) { 'NUL' } else { '/dev/null' }
    $sshCommonArgs += @('-o', 'StrictHostKeyChecking=no', '-o', "UserKnownHostsFile=$nullDevice")
    $scpCommonArgs += @('-o', 'StrictHostKeyChecking=no', '-o', "UserKnownHostsFile=$nullDevice")
}

$sshTarget = "$User@$Host"
$scpTarget = "{0}:{1}" -f $sshTarget, $remoteArtifactPath

Invoke-External -FilePath 'scp' -Arguments ($scpCommonArgs + @($resolvedArtifactPath, $scpTarget))

$remoteHashCommand = "sha256sum " + (ConvertTo-ShellLiteral -Value $remoteArtifactPath) + " | awk '{print `$1}'"
$remoteHash = (Invoke-External -FilePath 'ssh' -Arguments ($sshCommonArgs + @($sshTarget, $remoteHashCommand)) -CaptureOutput).Trim().ToLowerInvariant()
if ($remoteHash -ne $artifactSha256) {
    throw "Remote artifact hash mismatch. local=$artifactSha256 remote=$remoteHash"
}

$deployCommand = @(
    'sudo',
    (ConvertTo-ShellLiteral -Value $RemoteDeployScript),
    '--artifact-path',
    (ConvertTo-ShellLiteral -Value $remoteArtifactPath),
    '--repo-ref',
    (ConvertTo-ShellLiteral -Value $RepoRef)
) -join ' '

Invoke-External -FilePath 'ssh' -Arguments ($sshCommonArgs + @($sshTarget, $deployCommand))

$auditCommand = 'sudo tail -n 1 ' + (ConvertTo-ShellLiteral -Value $RemoteAuditLogPath)
$auditLine = (Invoke-External -FilePath 'ssh' -Arguments ($sshCommonArgs + @($sshTarget, $auditCommand)) -CaptureOutput).Trim()

$result = [pscustomobject]@{
    host                 = $Host
    user                 = $User
    port                 = $Port
    repo_ref             = $RepoRef
    artifact_name        = $artifactName
    artifact_sha256      = $artifactSha256
    remote_artifact_path = $remoteArtifactPath
    deployed_at_utc      = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    remote_audit_line    = $auditLine
}

if ($AuditOutputPath) {
    $resolvedAuditOutputPath = Resolve-NormalisedPath -Path $AuditOutputPath
    $auditOutputDir = Split-Path -Parent $resolvedAuditOutputPath
    if ($auditOutputDir) {
        New-Item -ItemType Directory -Force -Path $auditOutputDir | Out-Null
    }
    $result | ConvertTo-Json -Depth 8 | Set-Content -Path $resolvedAuditOutputPath -Encoding UTF8
}

$result | ConvertTo-Json -Depth 8
