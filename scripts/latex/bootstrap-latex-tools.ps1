[CmdletBinding()]
param(
    [switch]$Minimal,
    [switch]$SkipGuiTools
)

$ErrorActionPreference = "Stop"

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-Administrator {
    if (-not (Test-Administrator)) {
        throw "Run this script in an Administrator PowerShell session."
    }
}

function Assert-WingetAvailable {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is not available on this machine."
    }
}

function Test-WingetPackageInstalled {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Id
    )

    $output = winget list --id $Id --exact --accept-source-agreements 2>$null
    if ($LASTEXITCODE -ne 0) {
        return $false
    }

    return [bool]($output -match [regex]::Escape($Id))
}

function Install-WingetPackageIfMissing {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Id,

        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if (Test-WingetPackageInstalled -Id $Id) {
        Write-Host "==> $Name already installed ($Id)" -ForegroundColor DarkGreen
        return
    }

    Write-Host "==> Installing $Name ($Id)..." -ForegroundColor Cyan

    winget install `
        --id $Id `
        --exact `
        --scope machine `
        --accept-package-agreements `
        --accept-source-agreements `
        --disable-interactivity

    if ($LASTEXITCODE -ne 0) {
        throw "winget install failed for $Id"
    }
}

function Refresh-SessionPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Add-DirectoriesToMachinePath {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Directories
    )

    $currentMachinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $pathEntries = @()
    if ($currentMachinePath) {
        $pathEntries = $currentMachinePath -split ';' | Where-Object { $_ }
    }

    $changed = $false
    foreach ($directory in $Directories) {
        if (-not $directory -or -not (Test-Path $directory)) {
            continue
        }

        $fullDirectory = [System.IO.Path]::GetFullPath($directory)
        $alreadyPresent = $pathEntries | Where-Object {
            [string]::Equals($_.TrimEnd('\'), $fullDirectory.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase)
        }

        if ($alreadyPresent) {
            continue
        }

        Write-Host "==> Adding to machine PATH: $fullDirectory" -ForegroundColor Cyan
        $pathEntries += $fullDirectory
        $changed = $true
    }

    if (-not $changed) {
        return
    }

    [Environment]::SetEnvironmentVariable("Path", ($pathEntries -join ';'), "Machine")
}

function Find-FirstExistingDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Candidates
    )

    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }

    return $null
}

function Update-GlobalLatexToolPath {
    $directoriesToAdd = @()

    $miktexBin = Find-FirstExistingDirectory -Candidates @(
        "$env:ProgramFiles\MiKTeX\miktex\bin\x64",
        "$env:ProgramFiles\MiKTeX\miktex\bin",
        "$env:ProgramFiles(x86)\MiKTeX\miktex\bin\x64",
        "$env:ProgramFiles(x86)\MiKTeX\miktex\bin",
        "$env:LOCALAPPDATA\Programs\MiKTeX\miktex\bin\x64",
        "$env:LOCALAPPDATA\Programs\MiKTeX\miktex\bin"
    )
    if ($miktexBin) {
        $directoriesToAdd += $miktexBin
    }

    $strawberryPerlBin = Find-FirstExistingDirectory -Candidates @(
        "C:\Strawberry\perl\bin",
        "$env:ProgramFiles\Strawberry Perl\perl\bin"
    )
    if ($strawberryPerlBin) {
        $directoriesToAdd += $strawberryPerlBin
    }

    $strawberryCbin = Find-FirstExistingDirectory -Candidates @(
        "C:\Strawberry\c\bin",
        "$env:ProgramFiles\Strawberry Perl\c\bin"
    )
    if ($strawberryCbin) {
        $directoriesToAdd += $strawberryCbin
    }

    Add-DirectoriesToMachinePath -Directories $directoriesToAdd
}

function Find-MikTeXPackageManager {
    $roots = @(
        "$env:ProgramFiles\MiKTeX",
        "$env:ProgramFiles(x86)\MiKTeX",
        "$env:LOCALAPPDATA\Programs\MiKTeX",
        "$env:LOCALAPPDATA\MiKTeX"
    ) | Where-Object { $_ -and (Test-Path $_) }

    foreach ($root in $roots) {
        $mpm = Get-ChildItem -Path $root -Recurse -Filter "mpm.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName
        if ($mpm) {
            return $mpm
        }
    }

    return $null
}

function Install-MikTeXPackages {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$PackageNames
    )

    $mpm = Find-MikTeXPackageManager
    if (-not $mpm) {
        Write-Warning "Could not find MiKTeX package manager (mpm.exe)."
        Write-Warning "If latexmk or chktex are missing, install them later in MiKTeX Console (Admin)."
        return
    }

    Write-Host "==> Found MiKTeX package manager: $mpm" -ForegroundColor Cyan

    try {
        & $mpm --admin --update-db
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "MiKTeX package database update returned exit code $LASTEXITCODE."
        }

        foreach ($packageName in $PackageNames) {
            Write-Host "==> Installing MiKTeX package: $packageName" -ForegroundColor Cyan
            & $mpm --admin --install=$packageName
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "MiKTeX package install returned exit code $LASTEXITCODE for $packageName."
            }
        }
    }
    catch {
        Write-Warning "MiKTeX package-manager step failed: $($_.Exception.Message)"
        Write-Warning "If needed, open MiKTeX Console (Admin) and install the packages manually."
    }
}

function Configure-MikTeXAutoInstall {
    $initexmf = Get-Command initexmf -ErrorAction SilentlyContinue
    if (-not $initexmf) {
        Write-Warning "Could not find initexmf. MiKTeX auto-install was not configured."
        return
    }

    try {
        Write-Host "==> Configuring MiKTeX to auto-install missing packages" -ForegroundColor Cyan

        & $initexmf.Source --admin --set-config-value "[MPM]AutoInstall=yes"
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Setting MiKTeX AutoInstall=yes returned exit code $LASTEXITCODE."
        }

        & $initexmf.Source --admin --update-fndb
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "MiKTeX file name database update returned exit code $LASTEXITCODE."
        }
    }
    catch {
        Write-Warning "Failed to configure MiKTeX auto-install: $($_.Exception.Message)"
    }
}

function Show-ToolStatus {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$ToolNames
    )

    $rows = foreach ($toolName in $ToolNames) {
        $command = Get-Command $toolName -ErrorAction SilentlyContinue
        [PSCustomObject]@{
            Tool   = $toolName
            Found  = [bool]$command
            Source = if ($command) { $command.Source } else { $null }
        }
    }

    $rows | Format-Table -AutoSize
}

function Show-ToolVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        return
    }

    Write-Host "`n[$Name]" -ForegroundColor Yellow
    & $Command
}

Assert-Administrator
Assert-WingetAvailable

$basePackages = @(
    @{ Id = "MiKTeX.MiKTeX"; Name = "MiKTeX" },
    @{ Id = "StrawberryPerl.StrawberryPerl"; Name = "Strawberry Perl" }
)

$extraPackages = @(
    @{ Id = "JohnMacFarlane.Pandoc"; Name = "Pandoc" }
)

$guiPackages = @(
    @{ Id = "SumatraPDF.SumatraPDF"; Name = "SumatraPDF" },
    @{ Id = "TeXstudio.TeXstudio"; Name = "TeXstudio" }
)

$packagesToInstall = @($basePackages)
if (-not $Minimal) {
    $packagesToInstall += $extraPackages
}
if (-not $SkipGuiTools) {
    $packagesToInstall += $guiPackages
}

Write-Host "Installing LaTeX toolchain packages..." -ForegroundColor Green
foreach ($package in $packagesToInstall) {
    Install-WingetPackageIfMissing -Id $package.Id -Name $package.Name
}

Update-GlobalLatexToolPath
Refresh-SessionPath

Install-MikTeXPackages -PackageNames @("latexmk", "chktex")
Configure-MikTeXAutoInstall

Refresh-SessionPath

Write-Host "`n==> Toolchain status" -ForegroundColor Green
Show-ToolStatus -ToolNames @(
    "pdflatex",
    "bibtex",
    "perl",
    "pandoc",
    "latexmk",
    "chktex"
)

Write-Host "`n==> Version checks" -ForegroundColor Green
Show-ToolVersion -Name "pdflatex" -Command { pdflatex --version | Select-Object -First 2 }
Show-ToolVersion -Name "bibtex" -Command { bibtex --version | Select-Object -First 1 }
Show-ToolVersion -Name "perl" -Command { perl --version | Select-Object -First 2 }
Show-ToolVersion -Name "pandoc" -Command { pandoc --version | Select-Object -First 2 }
Show-ToolVersion -Name "latexmk" -Command { latexmk -v | Select-Object -First 2 }
Show-ToolVersion -Name "chktex" -Command { chktex --version | Select-Object -First 1 }

Write-Host "`nDone." -ForegroundColor Green
Write-Host "Open a new PowerShell session if any command is still missing from PATH." -ForegroundColor Green
Write-Host "`nExample compile:" -ForegroundColor Green
Write-Host "  & `"$PSScriptRoot\compile-paper.ps1`" -OpenPdf" -ForegroundColor Green
