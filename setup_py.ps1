<#
.SYNOPSIS
    Setup script for configuring Python environment with PDM and VS Code settings.

.DESCRIPTION
    This script creates a Python virtual environment, installs PDM within it,
    installs required packages, and optionally configures VS Code settings.
    It allows specifying a target Python minor version.

.PARAMETER ConfigureVSCode
    Optional flag to configure VS Code settings.

.PARAMETER Reset
    Optional flag to remove created files and directories and start fresh.

.PARAMETER PythonVersion
    Optional string to specify the target Python minor version (e.g., "3.12").
    If not provided, the script searches for a compatible version (3.10-3.15).

.PARAMETER SkipMongoDB
    Optional flag to skip MongoDB installation checks and continue setup.

.EXAMPLES
    ./setup_py.ps1
    ./setup_py.ps1 -ConfigureVSCode
    ./setup_py.ps1 -Reset
    ./setup_py.ps1 -ConfigureVSCode -PythonVersion 3.12
    ./setup_py.ps1 -Reset -ConfigureVSCode -PythonVersion 3.11
    ./setup_py.ps1 -SkipMongoDB -ConfigureVSCode
#>
param (
    [Parameter(Mandatory = $false)]
    [switch]$ConfigureVSCode, # Renamed from VSCODE_FLAG

    [Parameter(Mandatory = $false)]
    [switch]$Reset, # Renamed from RESET_FLAG

    [Parameter(Mandatory = $false)]
    [string]$PythonVersion,

    [Parameter(Mandatory = $false)]
    [switch]$SkipMongoDB
)

# Function to reset the environment
function Reset-Environment {
    Write-Host "=== Resetting Environment ===" -ForegroundColor Cyan


    # Remove the virtual environment directory with robust error handling
    if (Test-Path ".venv") {
        try {
            Remove-Item -Recurse -Force ".venv" -ErrorAction Stop
            Write-Host "Removed .venv directory." -ForegroundColor Green
        }
        catch {
            Write-Host "Error: Failed to remove the '.venv' directory." -ForegroundColor Red
            Write-Host "This is likely because a process (like VS Code, a terminal, or Python) is using it." -ForegroundColor Yellow
            Write-Host "Please close all terminals, editors, or processes that might be using the virtual environment and try again." -ForegroundColor Yellow
            Write-Host "Full error: $($_.Exception.Message)" -ForegroundColor DarkGray
            exit 1
        }
    }
    else {
        Write-Host ".venv directory does not exist. Skipping removal." -ForegroundColor Yellow
    }

    # Remove the .vscode directory
    if (Test-Path ".vscode") {
        Remove-Item -Recurse -Force ".vscode"
        Write-Host "Removed .vscode directory." -ForegroundColor Green
    }
    else {
        Write-Host ".vscode directory does not exist. Skipping removal." -ForegroundColor Yellow
    }
}

# Function to configure VS Code settings
function Set-VSCode {
    Write-Host "Setting up VS Code settings..." -ForegroundColor Cyan

    # Create .vscode directory if it doesn't exist
    $vscodeDir = ".vscode"
    if (-not (Test-Path $vscodeDir)) {
        New-Item -ItemType Directory -Path $vscodeDir | Out-Null
    }

    # Create or update settings.json
    $settingsFile = Join-Path $vscodeDir "settings.json"
    $settings = @{}

    if (-not (Test-Path $settingsFile)) {
        # Fresh file – create hashtable directly
        $settings = @{}
        $settings["python.defaultInterpreterPath"] = ".venv\Scripts\python.exe"
        $settings["terminal.integrated.defaultProfile.windows"] = "PowerShell"
        $settings | ConvertTo-Json -Depth 10 | Set-Content -Path $settingsFile
        Write-Host "Created $settingsFile" -ForegroundColor Green
    }
    else {
        # Load existing JSON (may deserialize to PSCustomObject) and convert to hashtable
        try {
            $raw = Get-Content -Raw $settingsFile | ConvertFrom-Json
        }
        catch {
            Write-Host "Warning: Failed to parse existing settings.json, backing it up and recreating." -ForegroundColor Yellow
            Copy-Item $settingsFile "$settingsFile.bak_$(Get-Date -Format yyyyMMddHHmmss)" -ErrorAction SilentlyContinue
            $raw = $null
        }

        $settings = @{}
        if ($raw) {
            $raw.PSObject.Properties | ForEach-Object { $settings[$_.Name] = $_.Value }
        }

        Write-Host "$settingsFile already exists. Updating..." -ForegroundColor Yellow

        # Set / update targeted keys
        $settings["python.defaultInterpreterPath"] = ".venv\Scripts\python.exe"
        $settings["terminal.integrated.defaultProfile.windows"] = "PowerShell"

        $settings | ConvertTo-Json -Depth 10 | Set-Content -Path $settingsFile
        Write-Host "VS Code settings updated successfully." -ForegroundColor Green
    }
}

# Function to find compatible Python version
function Find-Python {
    param(
        [string]$TargetVersion = $null
    )

    Write-Host "Detecting Python installation..." -ForegroundColor Cyan
    # Use py.exe from the default location if it exists, otherwise search PATH
    $pyLauncher = Join-Path $env:SystemRoot 'py.exe'
    if (-not (Test-Path $pyLauncher)) {
        $pyCmdInfo = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($pyCmdInfo) {
            $pyLauncher = $pyCmdInfo.Source
        }
    }
    if (-not $pyLauncher) {
        Write-Host "Python Launcher ('py.exe') not found. Trying 'python', 'python3', and specific versions." -ForegroundColor Yellow
    }

    # If a specific version is requested, try that first
    if (-not [string]::IsNullOrEmpty($TargetVersion)) {
        Write-Host "Attempting to find specified Python version: $TargetVersion" -ForegroundColor Magenta
        if ($pyLauncher) {
            try {
                $pyVersionInfo = & $pyLauncher "-$TargetVersion" --version 2>&1
                if ($LASTEXITCODE -eq 0 -and $pyVersionInfo -match "Python $TargetVersion") {
                    Write-Host "Found specified version: $pyVersionInfo via 'py -$TargetVersion'" -ForegroundColor Green
                    return "$pyLauncher", "-$TargetVersion"
                }
                else {
                    Write-Host "Specified Python version $TargetVersion not found via 'py -$TargetVersion'. Trying direct command 'python$TargetVersion'." -ForegroundColor Yellow
                }
            }
            catch {
                Write-Host "Error executing 'py -$TargetVersion --version'. Trying direct command 'python$TargetVersion'." -ForegroundColor Yellow
            }
        }
        # Try direct command like python3.12
        try {
            $pythonCmd = "python$TargetVersion"
            $pyVersionInfo = & $pythonCmd --version 2>&1
            if ($LASTEXITCODE -eq 0 -and $pyVersionInfo -match "Python $TargetVersion") {
                Write-Host "Found specified version: $pyVersionInfo via '$pythonCmd'" -ForegroundColor Green
                return $pythonCmd, ""
            }
            else {
                Write-Host "Error: Specified Python version $TargetVersion not found via '$pythonCmd' either." -ForegroundColor Red
                Write-Host "Please ensure Python $TargetVersion is installed and accessible." -ForegroundColor Red
                exit 1
            }
        }
        catch {
            Write-Host "Error: Specified Python version $TargetVersion not found via 'py -$TargetVersion' or '$pythonCmd'." -ForegroundColor Red
            Write-Host "Please ensure Python $TargetVersion is installed and accessible." -ForegroundColor Red
            exit 1
        }
    }

    # --- Default search logic if no specific version is requested ---
    Write-Host "No specific version requested, searching for compatible Python (3.10-3.15)..." -ForegroundColor Cyan

    # Try using py launcher first (Windows Python Launcher)
    if ($pyLauncher) {
        try {
            # Try generic -3 first
            $pyVersionInfo = & $pyLauncher -3 --version 2>&1
            if ($pyVersionInfo -match "Python 3\.(10|11|12|13|14|15)") {
                Write-Host "Found matching $pyVersionInfo via Python Launcher ('py -3')" -ForegroundColor Green
                return "$pyLauncher", "-3"
            }

            # Try specific versions with py launcher if generic -3 didn't match expected range
            foreach ($version in @("3.15", "3.14", "3.13", "3.12", "3.11", "3.10")) {
                try {
                    $pyVersionInfo = & $pyLauncher -$version --version 2>&1
                    if ($pyVersionInfo -match "Python $version") {
                        Write-Host "Found specific $pyVersionInfo via Python Launcher ('py -$version')" -ForegroundColor Green
                        return "$pyLauncher", "-$version"
                    }
                }
                catch {} # Ignore errors for specific versions if they don't exist
            }
            Write-Host "Python Launcher ('py.exe') found, but no compatible version (3.10-3.15) detected via it." -ForegroundColor Yellow
        }
        catch {
            # This catch might not be strictly necessary if the initial check for $pyLauncher worked, but good for robustness
            Write-Host "Error executing Python Launcher ('py.exe')." -ForegroundColor Yellow
        }
    }

    # Try regular python command
    try {
        $pythonVersionInfo = & python --version 2>&1
        if ($pythonVersionInfo -match "Python 3\.(10|11|12|13|14|15)") {
            Write-Host "Found $pythonVersionInfo via 'python'" -ForegroundColor Green
            return "python", ""
        }
    }
    catch {}

    # Try python3 command
    try {
        $python3VersionInfo = & python3 --version 2>&1
        if ($python3VersionInfo -match "Python 3\.(10|11|12|13|14|15)") {
            Write-Host "Found $python3VersionInfo via 'python3'" -ForegroundColor Green
            return "python3", ""
        }
    }
    catch {}

    # Try specific python3.X commands
    Write-Host "Trying specific version commands (python3.10, python3.11, etc.)..." -ForegroundColor Cyan
    foreach ($version in @("3.15", "3.14", "3.13", "3.12", "3.11", "3.10")) {
        $pythonCmd = "python$version"
        try {
            $pyVersionInfo = & $pythonCmd --version 2>&1
            if ($pyVersionInfo -match "Python $version") {
                Write-Host "Found $pyVersionInfo via '$pythonCmd'" -ForegroundColor Green
                return $pythonCmd, ""
            }
        }
        catch {} # Ignore errors if the specific command doesn't exist
    }

    Write-Host "Error: Compatible Python (3.10-3.15) not found via py.exe, python, python3, or python3.X." -ForegroundColor Red
    Write-Host "Please install a compatible Python version from https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}

# Function to initialize Windows environment with PDM
function Initialize-Windows {
    param(
        [string]$RequestedPythonVersion = $null # Accept the requested version
    )
    Write-Host "=== Running Windows Setup ===" -ForegroundColor Cyan

    # Find Python, passing the requested version if provided
    $pythonCmd, $pythonExec = Find-Python -TargetVersion $RequestedPythonVersion
    Write-Host "Using Python command:  $pythonCmd" -ForegroundColor Green
    if ($pythonExec) {
        Write-Host "Using Python exec arg: $pythonExec" -ForegroundColor Green
        Write-Host "Effective command:   '$pythonCmd $pythonExec'" -ForegroundColor Green
    }
    else {
        Write-Host "Effective command:   '$pythonCmd'" -ForegroundColor Green
    }

    # Create a virtual environment if it doesn't exist
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Write-Host "Creating virtual environment..." -ForegroundColor Cyan
        if ($pythonExec) {
            & $pythonCmd $pythonExec -m venv .venv
        }
        else {
            & $pythonCmd -m venv .venv
        }

        if (-not (Test-Path ".venv\Scripts\python.exe")) {
            Write-Host "Error: Failed to create virtual environment." -ForegroundColor Red
            exit 1
        }
    }
    else {
        Write-Host "Virtual environment already exists. Skipping creation." -ForegroundColor Yellow
    }

    # Ensure pip is updated in the virtual environment
    Write-Host "Updating pip in virtual environment..." -ForegroundColor Green
    & .venv\Scripts\python -m pip install --upgrade pip

    # Install PDM in the virtual environment if not already installed
    $pdmInstalled = & .venv\Scripts\python -m pip show pdm 2>$null
    if (-not $pdmInstalled) {
        Write-Host "Installing PDM in virtual environment..." -ForegroundColor Cyan
        & .venv\Scripts\python -m pip install pdm
    }
    else {
        Write-Host "PDM is already installed in the virtual environment. Skipping installation." -ForegroundColor Yellow
    }

    # Initialize PDM project if needed
    if (-not (Test-Path "pyproject.toml")) {
        Write-Host "Initializing PDM project..." -ForegroundColor Cyan
        & .venv\Scripts\pdm init --non-interactive
    }
    else {
        Write-Host "pyproject.toml already exists. Skipping PDM project initialization." -ForegroundColor Yellow
    }

    # Install dependencies with PDM
    Write-Host "Installing dependencies with PDM..." -ForegroundColor Cyan
    & .venv\Scripts\pdm install --verbose

    # Verify installation
    if (Test-Path ".venv\Lib\site-packages") {
        Write-Host "PDM successfully installed packages." -ForegroundColor Green
    }
    else {
        Write-Host "Error: Package installation failed." -ForegroundColor Red
        exit 1
    }
}

# Function to ensure uv and arxiv-mcp-server are installed
function Ensure-UvAndArxiv {
    Write-Host "Checking for uv installation..." -ForegroundColor Cyan

    # Check if uv is installed
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uvCmd) {
        Write-Host "uv not found. Installing uv..." -ForegroundColor Yellow
        try {
            # Install uv using the official installer
            Invoke-WebRequest -Uri "https://astral.sh/uv/install.ps1" -UseBasicParsing | Invoke-Expression
            Write-Host "uv installed successfully." -ForegroundColor Green
        }
        catch {
            Write-Host "Warning: Failed to install uv automatically. You may need to install it manually from https://docs.astral.sh/uv/" -ForegroundColor Yellow
            Write-Host "Error: $($_.Exception.Message)" -ForegroundColor DarkGray
            return
        }
    }
    else {
        Write-Host "uv is already installed at $($uvCmd.Source)" -ForegroundColor Green
    }

    # Add uv tools directory to PATH if not already there
    $uvToolsPath = "$env:USERPROFILE\.local\bin"
    if ($env:PATH -notlike "*$uvToolsPath*") {
        Write-Host "Adding uv tools directory to PATH for this session..." -ForegroundColor Cyan
        $env:PATH = "$uvToolsPath;$env:PATH"
    }

    # Check if arxiv-mcp-server is installed
    Write-Host "Checking for arxiv-mcp-server..." -ForegroundColor Cyan
    try {
        $uvToolList = uv tool list 2>&1
        if ($uvToolList -match "arxiv-mcp-server") {
            Write-Host "arxiv-mcp-server is already installed." -ForegroundColor Green
        }
        else {
            Write-Host "Installing arxiv-mcp-server..." -ForegroundColor Yellow
            uv tool install arxiv-mcp-server
            Write-Host "arxiv-mcp-server installed successfully." -ForegroundColor Green
        }
    }
    catch {
        Write-Host "Warning: Failed to check or install arxiv-mcp-server. You may need to run 'uv tool install arxiv-mcp-server' manually." -ForegroundColor Yellow
        Write-Host "Error: $($_.Exception.Message)" -ForegroundColor DarkGray
    }
}

# Function to check and create .env file from template
function Ensure-EnvFile {
    Write-Host ""
    Write-Host "=== Database Configuration ===" -ForegroundColor Cyan

    if (-not (Test-Path ".env")) {
        Write-Host "No .env file found. Creating from template..." -ForegroundColor Yellow

        if (Test-Path ".env.template") {
            Copy-Item ".env.template" ".env"
            Write-Host "✓ Created .env from .env.template" -ForegroundColor Green
            Write-Host "  You can edit .env to customize database and API settings" -ForegroundColor Gray
        } elseif (Test-Path ".env.example") {
            Copy-Item ".env.example" ".env"
            Write-Host "✓ Created .env from .env.example" -ForegroundColor Green
            Write-Host "  You can edit .env to customize database and API settings" -ForegroundColor Gray
        } else {
            Write-Host "✗ No .env template found. Creating minimal .env..." -ForegroundColor Yellow
            @"
MONGO_URI=mongodb://localhost:27017/
VON_DB_NAME=von_db
MONGO_LOCAL_URI=mongodb://127.0.0.1:27017/?directConnection=true
MONGO_ALLOW_LOCAL_FALLBACK=1
OLLAMA_HOSTS_LIST=127.0.0.1
"@ | Out-File -FilePath ".env" -Encoding UTF8
            Write-Host "✓ Created minimal .env file" -ForegroundColor Green
        }
    } else {
        Write-Host ".env file already exists. Skipping creation." -ForegroundColor Green
    }


}

# Function to check and install MongoDB (Windows)
function Ensure-MongoDB {
    Write-Host "Checking for MongoDB installation..." -ForegroundColor Cyan

    # Try to find mongod.exe in PATH
    $mongod = Get-Command mongod.exe -ErrorAction SilentlyContinue
    if ($mongod) {
        Write-Host "MongoDB is already installed at $($mongod.Source)" -ForegroundColor Green
        return
    }

    # Check default install locations if not found in PATH
    $defaultDirs = @(
        "C:\Program Files\MongoDB\Server",
        "C:\Program Files (x86)\MongoDB\Server"
    )
    $foundMongo = $false
    foreach ($baseDir in $defaultDirs) {
        if (Test-Path $baseDir) {
            $versions = Get-ChildItem -Path $baseDir -Directory | Sort-Object Name -Descending
            foreach ($ver in $versions) {
                $mongoExe = Join-Path $ver.FullName "bin\mongod.exe"
                if (Test-Path $mongoExe) {
                    Write-Host "MongoDB found at $mongoExe (not in PATH). Adding to PATH for this session." -ForegroundColor Yellow
                    $binPath = Join-Path $ver.FullName "bin"
                    $env:Path = $env:Path + ";" + $binPath
                    $foundMongo = $true
                    break
                }
            }
        }
        if ($foundMongo) { break }
    }
    if ($foundMongo) { return }

    Write-Host "MongoDB not found. Attempting to install MongoDB Community Edition..." -ForegroundColor Yellow

    # Download MongoDB MSI installer
    $mongoVersion = "7.0.9"
    $arch = if ([Environment]::Is64BitOperatingSystem) { "x86_64" } else { "x86" }
    $msiUrl = "https://fastdl.mongodb.org/windows/mongodb-windows-$arch-$mongoVersion-signed.msi"
    $msiPath = "$env:TEMP\mongodb-$mongoVersion.msi"

    # Only download if the installer is missing or older than 1 day
    $downloadInstaller = $true
    if (Test-Path $msiPath) {
        $fileAge = (Get-Date) - (Get-Item $msiPath).LastWriteTime
        if ($fileAge.TotalDays -lt 1) {
            Write-Host "MongoDB installer already downloaded and is less than a day old. Skipping download." -ForegroundColor Green
            $downloadInstaller = $false
        }
        else {
            Write-Host "MongoDB installer exists but is older than 1 day. Re-downloading..." -ForegroundColor Yellow
        }
    }
    if ($downloadInstaller) {
        Write-Host "Downloading MongoDB installer from $msiUrl ..." -ForegroundColor Cyan
        try {
            Invoke-WebRequest -Uri $msiUrl -OutFile $msiPath -UseBasicParsing
        }
        catch {
            Write-Host "Failed to download MongoDB installer. Please download and install manually from https://www.mongodb.com/try/download/community" -ForegroundColor Red
            exit 1
        }
    }

    Write-Host "Running MongoDB installer..." -ForegroundColor Cyan
    $installArgs = "/qn INSTALLLOCATION=`"C:\Program Files\MongoDB\Server\$mongoVersion`" ADDLOCAL=All"
    $process = Start-Process msiexec.exe -ArgumentList "/i `"$msiPath`" $installArgs" -Wait -PassThru

    if ($process.ExitCode -eq 0) {
        Write-Host "MongoDB installed successfully." -ForegroundColor Green
    }
    elseif ($process.ExitCode -eq 1603) {
        Write-Host "MongoDB installer returned exit code 1603. This usually means MongoDB is already installed or a conflicting version exists." -ForegroundColor Yellow
    }
    else {
        Write-Host "MongoDB installation failed with exit code $($process.ExitCode)." -ForegroundColor Red
        Write-Host "Please install MongoDB manually from https://www.mongodb.com/try/download/community" -ForegroundColor Red
        exit 1
    }

    # After install, check again and add to PATH if found
    $foundMongo = $false
    # Check default install locations
    foreach ($baseDir in $defaultDirs) {
        if (Test-Path $baseDir) {
            $versions = Get-ChildItem -Path $baseDir -Directory | Sort-Object Name -Descending
            foreach ($ver in $versions) {
                $mongoExe = Join-Path $ver.FullName "bin\mongod.exe"
                if (Test-Path $mongoExe) {
                    Write-Host "MongoDB found at $mongoExe after install. Adding to PATH for this session." -ForegroundColor Yellow
                    $binPath = Join-Path $ver.FullName "bin"
                    $env:Path = $env:Path + ";" + $binPath
                    $foundMongo = $true
                    break
                }
            }
        }
        if ($foundMongo) { break }
    }
    # Check PATH again in case the installer added it
    if (-not $foundMongo) {
        $mongod = Get-Command mongod.exe -ErrorAction SilentlyContinue
        if ($mongod) {
            Write-Host "MongoDB found in PATH after install at $($mongod.Source)." -ForegroundColor Green
            $foundMongo = $true
        }
    }
    if ($foundMongo) { return }

    Write-Host "MongoDB still not found after installer attempt." -ForegroundColor Red
    Write-Host "The automated MongoDB installation failed or did not result in a detectable installation." -ForegroundColor Yellow
    Write-Host "This might be due to permissions issues or other conflicts. The installer exit code was $($process.ExitCode)." -ForegroundColor Yellow
    Write-Host "Please try installing MongoDB manually using the downloaded installer with Administrator privileges:" -ForegroundColor Cyan
    Write-Host "1. Open PowerShell as Administrator." -ForegroundColor Cyan
    Write-Host "2. Run the following command:" -ForegroundColor Cyan
    Write-Host "   msiexec /i `"$msiPath`"" -ForegroundColor White
    Write-Host "   (Installer path: $msiPath)" -ForegroundColor DarkGray
    Write-Host "3. Follow the on-screen prompts in the installer. Choosing 'Network Service' for the service account is usually appropriate for local development." -ForegroundColor Cyan
    Write-Host "If the problem persists, check the MongoDB installation logs in your %TEMP% directory (search for MSI*.log files)." -ForegroundColor Yellow
    throw "MongoDB installation failed. Run setup with -SkipMongoDB to bypass this check."
}

## Validate parameters and provide an explanatory message
if (-not $ConfigureVSCode -and -not $Reset -and -not $PythonVersion) {
    # Check if any action parameter is provided
    Write-Host "No action parameters provided. Please specify one or more of the following parameters:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  -Reset            : Resets the environment by removing the .venv and .vscode directories." -ForegroundColor Cyan
    Write-Host "  -ConfigureVSCode  : Configures VS Code settings for the Python environment." -ForegroundColor Cyan
    Write-Host '  -PythonVersion <ver> : Specify the target Python minor version (e.g., "3.12").' -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Examples:" -ForegroundColor Yellow
    Write-Host "  ./setup_py.ps1 -Reset" -ForegroundColor Green
    Write-Host "      Resets the environment and exits." -ForegroundColor Gray
    Write-Host ""
    Write-Host "  ./setup_py.ps1 -ConfigureVSCode" -ForegroundColor Green
    Write-Host "      Sets up the Python environment (using detected Python) and configures VS Code." -ForegroundColor Gray
}

# Check if Reset is set
if ($Reset) {
    # Renamed from RESET_FLAG
    Reset-Environment
    # Check if ONLY reset was requested
    if (-not $ConfigureVSCode -and [string]::IsNullOrEmpty($PythonVersion)) {
        # Renamed from VSCODE_FLAG
        Write-Host "Environment reset complete. Exiting script as no setup flags were provided." -ForegroundColor Green
        exit 0
    }
}

# Main script logic
# Pass the PythonVersion parameter to Initialize-Windows

# Ensure MongoDB is installed before proceeding (unless skipped)
if (-not $SkipMongoDB) {
    try {
        Ensure-MongoDB
    }
    catch {
        Write-Host "MongoDB setup failed, but continuing with Python setup..." -ForegroundColor Yellow
        Write-Host "You can run setup later with -SkipMongoDB to bypass MongoDB checks." -ForegroundColor Yellow
    }
}
else {
    Write-Host "Skipping MongoDB installation checks (-SkipMongoDB flag set)." -ForegroundColor Yellow
}

# Ensure .env file exists (create from template if missing)
Ensure-EnvFile

# Ensure uv and arxiv-mcp-server are installed for MCP integrations
Ensure-UvAndArxiv

Initialize-Windows -RequestedPythonVersion $PythonVersion

if ($ConfigureVSCode) {
    # Renamed from VSCODE_FLAG
    Set-VSCode
}

Write-Host "=== Setup Complete! ===" -ForegroundColor Cyan
Write-Host 'To activate the virtual environment, run: .\.venv\Scripts\Activate.ps1' -ForegroundColor Green
