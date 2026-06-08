[CmdletBinding()]
param(
    [ValidateSet('init', 'fmt', 'validate', 'plan', 'apply', 'destroy')]
    [string]$Action = 'plan',

    [ValidateSet('dev', 'staging', 'prod')]
    [string]$Environment = 'dev',

    [string]$TerraformRoot,
    [string]$VarFile,
    [string]$PlanFile,
    [switch]$SkipInit,
    [switch]$AutoApprove,
    [switch]$AllowProdChanges
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-NormalisedPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Invoke-Terraform {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    Write-Host ("terraform {0}" -f ($Arguments -join ' ')) -ForegroundColor Cyan
    & terraform @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Terraform command failed with exit code $LASTEXITCODE."
    }
}

function Invoke-InitIfRequired {
    if ($SkipInit) {
        Write-Host "Skipping terraform init (-SkipInit)." -ForegroundColor Yellow
        return
    }

    Invoke-Terraform -Arguments @('init', '-input=false')
}

$repoRoot = Resolve-NormalisedPath (Join-Path $PSScriptRoot '..\..')

if (-not $TerraformRoot) {
    $TerraformRoot = Join-Path $repoRoot 'infra\openstack'
}

$TerraformRoot = Resolve-NormalisedPath $TerraformRoot
$templateVarFile = Join-Path $TerraformRoot ("environments\{0}\{0}.tfvars.example" -f $Environment)

if (-not $VarFile) {
    $VarFile = Join-Path $TerraformRoot ("environments\{0}\{0}.tfvars" -f $Environment)
}

$VarFile = Resolve-NormalisedPath $VarFile

if (-not $PlanFile) {
    $planDir = Join-Path $repoRoot '.run\terraform'
    New-Item -ItemType Directory -Path $planDir -Force | Out-Null
    $PlanFile = Join-Path $planDir ("{0}.tfplan" -f $Environment)
}

$PlanFile = Resolve-NormalisedPath $PlanFile

if (-not (Get-Command terraform -ErrorAction SilentlyContinue)) {
    throw "Terraform CLI not found on PATH. Install Terraform and retry."
}

if (($Action -eq 'apply' -or $Action -eq 'destroy') -and $Environment -eq 'prod' -and -not $AllowProdChanges) {
    throw "Refusing production $Action without -AllowProdChanges."
}

$requiresVarFile = @('validate', 'plan', 'apply', 'destroy') -contains $Action
if ($requiresVarFile -and -not (Test-Path -LiteralPath $VarFile)) {
    throw "Missing var file '$VarFile'. Copy and update template '$templateVarFile'."
}

Push-Location $TerraformRoot
try {
    switch ($Action) {
        'init' {
            Invoke-InitIfRequired
        }
        'fmt' {
            Invoke-Terraform -Arguments @('fmt', '-recursive')
        }
        'validate' {
            Invoke-InitIfRequired
            Invoke-Terraform -Arguments @('validate', '-no-color')

            # Trigger variable validations without mutating infrastructure state.
            $tempPlan = Join-Path ([System.IO.Path]::GetTempPath()) ("von_{0}_{1}.tfplan" -f $Environment, [guid]::NewGuid().ToString('N'))
            try {
                Invoke-Terraform -Arguments @(
                    'plan',
                    '-input=false',
                    '-refresh=false',
                    '-lock=false',
                    "-var-file=$VarFile",
                    "-out=$tempPlan"
                )
            }
            finally {
                if (Test-Path -LiteralPath $tempPlan) {
                    Remove-Item -LiteralPath $tempPlan -Force -ErrorAction SilentlyContinue
                }
            }
        }
        'plan' {
            Invoke-InitIfRequired
            Invoke-Terraform -Arguments @('validate', '-no-color')
            Invoke-Terraform -Arguments @(
                'plan',
                '-input=false',
                "-var-file=$VarFile",
                "-out=$PlanFile"
            )
            Write-Host "Plan file written to $PlanFile" -ForegroundColor Green
        }
        'apply' {
            Invoke-InitIfRequired
            Invoke-Terraform -Arguments @('validate', '-no-color')

            $applyArgs = @(
                'apply',
                '-input=false',
                "-var-file=$VarFile"
            )
            if ($AutoApprove) {
                $applyArgs += '-auto-approve'
            }

            Invoke-Terraform -Arguments $applyArgs
        }
        'destroy' {
            Invoke-InitIfRequired
            Invoke-Terraform -Arguments @('validate', '-no-color')

            $destroyArgs = @(
                'destroy',
                '-input=false',
                "-var-file=$VarFile"
            )
            if ($AutoApprove) {
                $destroyArgs += '-auto-approve'
            }

            Invoke-Terraform -Arguments $destroyArgs
        }
        default {
            throw "Unsupported action '$Action'."
        }
    }
}
finally {
    Pop-Location
}
