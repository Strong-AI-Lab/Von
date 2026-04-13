[CmdletBinding()]
param(
    [string]$TexFile = "docs/papers/von_systems_paper/von_systems_paper.tex",
    [switch]$OpenPdf,
    [switch]$Clean,
    [switch]$Lint,
    [switch]$ForcePdflatex
)

$ErrorActionPreference = "Stop"

function Resolve-RepoPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RelativeOrAbsolutePath
    )

    if ([System.IO.Path]::IsPathRooted($RelativeOrAbsolutePath)) {
        return [System.IO.Path]::GetFullPath($RelativeOrAbsolutePath)
    }

    $repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $RelativeOrAbsolutePath))
}

function Assert-CommandAvailable {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if (-not (Resolve-ToolCommand -Name $Name -AllowMissing)) {
        throw "Required command not found: $Name"
    }
}

function Resolve-ToolCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [switch]$AllowMissing
    )

    $resolved = Get-Command $Name -ErrorAction SilentlyContinue
    if ($resolved) {
        return $resolved.Source
    }

    $candidateDirectories = @(
        "$env:ProgramFiles\MiKTeX\miktex\bin\x64",
        "$env:ProgramFiles\MiKTeX\miktex\bin",
        "$env:ProgramFiles(x86)\MiKTeX\miktex\bin\x64",
        "$env:ProgramFiles(x86)\MiKTeX\miktex\bin",
        "C:\Strawberry\perl\bin",
        "C:\Strawberry\c\bin",
        "$env:ProgramFiles\Strawberry Perl\perl\bin",
        "$env:ProgramFiles\Strawberry Perl\c\bin",
        "$env:LOCALAPPDATA\Programs\MiKTeX\miktex\bin\x64",
        "$env:LOCALAPPDATA\Programs\MiKTeX\miktex\bin"
    ) | Where-Object { $_ -and (Test-Path $_) }

    $extensions = @(".exe", ".cmd", ".bat")
    foreach ($directory in $candidateDirectories) {
        foreach ($extension in $extensions) {
            $candidate = Join-Path $directory ($Name + $extension)
            if (Test-Path $candidate) {
                return $candidate
            }
        }
    }

    if ($AllowMissing) {
        return $null
    }

    throw "Required command not found: $Name"
}

function Add-DirectoryToPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Directory
    )

    if (-not (Test-Path $Directory)) {
        return
    }

    $pathEntries = $env:Path -split ';'
    if ($pathEntries -contains $Directory) {
        return
    }

    $env:Path = "$Directory;$env:Path"
}

function Initialize-LatexmkEnvironment {
    param(
        [string]$PerlPath
    )

    if (-not $PerlPath) {
        return
    }

    Add-DirectoryToPath -Directory (Split-Path -Parent $PerlPath)

    $strawberryCbin = Join-Path (Split-Path -Parent (Split-Path -Parent $PerlPath)) "c\bin"
    if (Test-Path $strawberryCbin) {
        Add-DirectoryToPath -Directory $strawberryCbin
    }
}

function Get-PdflatexFeatures {
    $pdflatex = Resolve-ToolCommand -Name "pdflatex"

    $versionLine = & $pdflatex --version 2>$null | Select-Object -First 1
    $isMiKTeX = ($LASTEXITCODE -eq 0) -and ($versionLine -match "MiKTeX")

    return [PSCustomObject]@{
        Path        = $pdflatex
        IsMiKTeX    = $isMiKTeX
        VersionLine = $versionLine
    }
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList,

        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory
    )

    $display = @($FilePath) + $ArgumentList
    Write-Host "==> $($display -join ' ')" -ForegroundColor Cyan

    Push-Location $WorkingDirectory
    try {
        & $FilePath @ArgumentList
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code ${LASTEXITCODE}: $($display -join ' ')"
        }
    }
    finally {
        Pop-Location
    }
}

function Remove-LatexBuildArtifacts {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Directory,

        [Parameter(Mandatory = $true)]
        [string]$BaseName
    )

    $extensions = @(
        "aux",
        "bbl",
        "bcf",
        "blg",
        "fdb_latexmk",
        "fls",
        "log",
        "lof",
        "lot",
        "out",
        "run.xml",
        "synctex.gz",
        "toc"
    )

    foreach ($extension in $extensions) {
        $path = Join-Path $Directory "$BaseName.$extension"
        if (Test-Path $path) {
            Remove-Item -LiteralPath $path -Force
        }
    }
}

$texPath = Resolve-RepoPath -RelativeOrAbsolutePath $TexFile
if (-not (Test-Path $texPath)) {
    throw "TeX file not found: $texPath"
}

$texDirectory = Split-Path -Parent $texPath
$texLeaf = Split-Path -Leaf $texPath
$baseName = [System.IO.Path]::GetFileNameWithoutExtension($texLeaf)
$pdfPath = Join-Path $texDirectory "$baseName.pdf"
$pdflatexFeatures = Get-PdflatexFeatures
$perlPath = Resolve-ToolCommand -Name "perl" -AllowMissing

if ($pdflatexFeatures.IsMiKTeX) {
    Write-Host "==> MiKTeX detected; enabling on-demand package installs during compile" -ForegroundColor Green
}

if ($Clean) {
    Write-Host "==> Cleaning previous LaTeX build artefacts" -ForegroundColor Green

    $latexmkPath = Resolve-ToolCommand -Name "latexmk" -AllowMissing
    if ($latexmkPath -and $perlPath -and -not $ForcePdflatex) {
        Initialize-LatexmkEnvironment -PerlPath $perlPath
        Invoke-CheckedCommand -FilePath $latexmkPath -ArgumentList @("-C", $texLeaf) -WorkingDirectory $texDirectory
    }
    elseif ($latexmkPath -and -not $perlPath -and -not $ForcePdflatex) {
        Write-Warning "latexmk is installed but perl was not found. Skipping latexmk clean step and removing build artefacts directly."
    }

    Remove-LatexBuildArtifacts -Directory $texDirectory -BaseName $baseName
}

if ($Lint) {
    $chktexPath = Resolve-ToolCommand -Name "chktex" -AllowMissing
    if ($chktexPath) {
        Invoke-CheckedCommand -FilePath $chktexPath -ArgumentList @("-q", "-wall", $texLeaf) -WorkingDirectory $texDirectory
    }
    else {
        Write-Warning "chktex is not installed; skipping lint step."
    }
}

$latexmkPath = Resolve-ToolCommand -Name "latexmk" -AllowMissing
if ($latexmkPath -and $perlPath -and -not $ForcePdflatex) {
    Initialize-LatexmkEnvironment -PerlPath $perlPath

    $latexmkArgs = @(
        "-pdf",
        "-interaction=nonstopmode",
        "-file-line-error",
        "-halt-on-error"
    )

    if ($pdflatexFeatures.IsMiKTeX) {
        $latexmkArgs += "-pdflatex=pdflatex --enable-installer %O %S"
    }

    $latexmkArgs += $texLeaf

    Invoke-CheckedCommand -FilePath $latexmkPath -ArgumentList $latexmkArgs -WorkingDirectory $texDirectory
}
else {
    if ($latexmkPath -and -not $perlPath -and -not $ForcePdflatex) {
        Write-Warning "latexmk is installed but perl was not found. Falling back to pdflatex + bibtex."
    }

    $bibtexPath = Resolve-ToolCommand -Name "bibtex"

    $pdflatexArgs = @("-interaction=nonstopmode", "-file-line-error")
    if ($pdflatexFeatures.IsMiKTeX) {
        $pdflatexArgs += "--enable-installer"
    }
    $pdflatexArgs += $texLeaf

    Invoke-CheckedCommand -FilePath $pdflatexFeatures.Path -ArgumentList $pdflatexArgs -WorkingDirectory $texDirectory
    Invoke-CheckedCommand -FilePath $bibtexPath -ArgumentList @($baseName) -WorkingDirectory $texDirectory
    Invoke-CheckedCommand -FilePath $pdflatexFeatures.Path -ArgumentList $pdflatexArgs -WorkingDirectory $texDirectory
    Invoke-CheckedCommand -FilePath $pdflatexFeatures.Path -ArgumentList $pdflatexArgs -WorkingDirectory $texDirectory
}

if (-not (Test-Path $pdfPath)) {
    throw "Compilation finished without producing the expected PDF: $pdfPath"
}

Write-Host "`nBuilt PDF:" -ForegroundColor Green
Write-Host $pdfPath -ForegroundColor Green

if ($OpenPdf) {
    Write-Host "==> Opening PDF" -ForegroundColor Green
    Start-Process -FilePath $pdfPath
}
