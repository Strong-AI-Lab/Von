#!/usr/bin/env bash
# V3.0.1
# This script is part of the Von-Private project and is specifically designed
# to work in the openAI Codex environment. This should mean it will work in
# most clean install Linux environments, but it is not guaranteed to work
# in all environments.
# Minimal setup script for Von-Private development environment
# Combines essential functionality from setup_py.ps1 and setup_js.sh
# Ensures both Python and JavaScript tests can run successfully
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}=== Von-Private Minimal Setup ===${NC}"

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to fix charset-normalizer issues
fix_charset_normalizer() {
    echo -e "${CYAN}Attempting to fix charset-normalizer permission issues...${NC}"

    # Remove the problematic .pyd file if it exists and is causing issues
    local charset_dir=""
    if [[ -d ".venv/lib/python*/site-packages/charset_normalizer" ]]; then
        charset_dir=".venv/lib/python*/site-packages/charset_normalizer"
    elif [[ -d ".venv/Lib/site-packages/charset_normalizer" ]]; then
        charset_dir=".venv/Lib/site-packages/charset_normalizer"
    fi

    if [[ -n "$charset_dir" ]]; then
        echo -e "${YELLOW}Removing problematic charset-normalizer files...${NC}"
        rm -rf "$charset_dir" 2>/dev/null || true
    fi

    # Reinstall with specific version
    $VENV_PYTHON -m pip install --force-reinstall --no-deps charset-normalizer==3.4.2
    echo -e "${GREEN}✓ charset-normalizer fixed${NC}"
}

# Function to find compatible Python version
find_python() {
    local python_cmd=""

    echo -e "${CYAN}Detecting Python installation...${NC}" >&2

    # Try different Python commands in order of preference
    for cmd in python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command_exists "$cmd"; then
            # Test if the command actually works (not just exists in PATH)
            if version=$($cmd --version 2>&1); then
                # Extract version number more robustly
                version_num=$(echo "$version" | grep -oE "3\.[0-9]+\.[0-9]+" | head -n1)
                if [[ -n "$version_num" ]]; then
                    major_minor=$(echo "$version_num" | cut -d. -f1,2)
                    minor_version=$(echo "$major_minor" | cut -d. -f2)

                    # Check if it's Python 3.10 or higher
                    if [[ "$major_minor" =~ ^3\.[0-9]+$ ]] && [[ "$minor_version" -ge 10 ]]; then
                        echo -e "${GREEN}Found compatible Python $version_num via '$cmd'${NC}" >&2
                        python_cmd="$cmd"
                        break
                    fi
                fi
            fi
        fi
    done

    # If no versioned python found, try to find python via pyenv or other version managers
    if [[ -z "$python_cmd" ]]; then
        echo -e "${YELLOW}Trying alternative Python detection methods...${NC}" >&2

        # Try pyenv if available
        if command_exists pyenv; then
            echo -e "${CYAN}Detected pyenv, trying to find compatible Python version...${NC}" >&2

            # Get available Python versions from pyenv
            available_versions=$(pyenv versions --bare 2>/dev/null | grep -E '^3\.(1[0-5]|[0-9]+)\.' | sort -V -r)

            for version in $available_versions; do
                major_minor=$(echo "$version" | cut -d. -f1,2)
                minor_version=$(echo "$major_minor" | cut -d. -f2)

                if [[ "$minor_version" -ge 10 ]]; then
                    # Test if we can use this version
                    if pyenv shell "$version" 2>/dev/null && python --version >/dev/null 2>&1; then
                        echo -e "${GREEN}Found compatible Python $version via pyenv${NC}" >&2
                        echo -e "${YELLOW}Setting pyenv to use Python $version${NC}" >&2
                        pyenv global "$version" 2>/dev/null || pyenv shell "$version"
                        python_cmd="python"
                        break
                    fi
                fi
            done
        fi
    fi

    if [[ -z "$python_cmd" ]]; then
        echo -e "${RED}Error: Compatible Python (3.10-3.15) not found${NC}" >&2
        echo -e "${RED}Please install Python 3.10+ from https://www.python.org/downloads/${NC}" >&2
        if command_exists pyenv; then
            echo -e "${YELLOW}Or install via pyenv: pyenv install 3.12.0 && pyenv global 3.12.0${NC}" >&2
        fi
        exit 1
    fi

    echo "$python_cmd"
}

# Check Node.js and npm for JavaScript tests
echo -e "${CYAN}Checking Node.js and npm...${NC}"
if ! command_exists node; then
    echo -e "${RED}Node.js is required but not found. Please install from https://nodejs.org/${NC}"
    exit 1
fi

if ! command_exists npm; then
    echo -e "${RED}npm is required but not found. Please install npm (comes with Node.js)${NC}"
    exit 1
fi

# Get versions safely
NODE_VERSION=$(node --version 2>/dev/null | head -n1)
NPM_VERSION=$(npm --version 2>/dev/null | head -n1)
echo -e "${GREEN}✓ Node.js ${NODE_VERSION} and npm ${NPM_VERSION} found${NC}"

# Find Python
PYTHON_CMD=$(find_python)

# Setup Python environment
echo -e "${CYAN}Setting up Python environment...${NC}"

# Create virtual environment if it doesn't exist
if [[ ! -f ".venv/bin/python" ]] && [[ ! -f ".venv/Scripts/python.exe" ]]; then
    echo -e "${CYAN}Creating virtual environment...${NC}"
    $PYTHON_CMD -m venv .venv

    if [[ ! -f ".venv/bin/python" ]] && [[ ! -f ".venv/Scripts/python.exe" ]]; then
        echo -e "${RED}Error: Failed to create virtual environment${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ Virtual environment created${NC}"
else
    echo -e "${YELLOW}Virtual environment already exists${NC}"
fi

# Determine the correct path for virtual environment binaries
if [[ -f ".venv/bin/python" ]]; then
    VENV_PYTHON=".venv/bin/python"
    VENV_PIP=".venv/bin/pip"
    VENV_PDM=".venv/bin/pdm"
else
    VENV_PYTHON=".venv/Scripts/python.exe"
    VENV_PIP=".venv/Scripts/pip.exe"
    VENV_PDM=".venv/Scripts/pdm.exe"
fi

# Upgrade pip
echo -e "${CYAN}Updating pip...${NC}"
$VENV_PYTHON -m pip install --upgrade pip

# Install PDM if not present
if ! $VENV_PYTHON -m pip show pdm >/dev/null 2>&1; then
    echo -e "${CYAN}Installing PDM...${NC}"
    $VENV_PYTHON -m pip install pdm
    echo -e "${GREEN}✓ PDM installed${NC}"
else
    echo -e "${YELLOW}PDM already installed${NC}"
fi

# Initialize PDM project if pyproject.toml doesn't exist
if [[ ! -f "pyproject.toml" ]]; then
    echo -e "${CYAN}Initializing PDM project...${NC}"
    $VENV_PDM init --non-interactive
    echo -e "${GREEN}✓ PDM project initialized${NC}"
else
    echo -e "${YELLOW}pyproject.toml already exists${NC}"
fi

# Install Python dependencies
echo -e "${CYAN}Installing Python dependencies...${NC}"
if $VENV_PDM install --verbose; then
    echo -e "${GREEN}✓ Python dependencies installed${NC}"
else
    echo -e "${YELLOW}PDM install failed, trying charset-normalizer fix...${NC}"

    # Handle charset-normalizer permission issues specifically
    fix_charset_normalizer

    # Install other critical packages with pip
    critical_packages="flask pymongo python-dotenv requests pytest pytest-mock pytest-cov mongomock"
    echo -e "${CYAN}Installing critical packages with pip...${NC}"
    $VENV_PYTHON -m pip install $critical_packages

    # Try PDM again with --no-sync to avoid version conflicts
    echo -e "${CYAN}Retrying PDM install...${NC}"
    if $VENV_PDM install --verbose --no-sync; then
        echo -e "${GREEN}✓ Python dependencies installed via fallback${NC}"
    else
        echo -e "${YELLOW}⚠ Some Python packages may not be installed correctly${NC}"
        echo -e "${YELLOW}  This is often caused by charset-normalizer permission issues on Windows${NC}"
        echo -e "${YELLOW}  Consider running: pip install --force-reinstall charset-normalizer==3.4.2${NC}"
    fi
fi

# Install JavaScript dependencies
echo -e "${CYAN}Installing JavaScript dependencies...${NC}"
npm install
echo -e "${GREEN}✓ JavaScript dependencies installed${NC}"

# Verify Python test dependencies
echo -e "${CYAN}Verifying Python test environment...${NC}"
test_packages="pytest pymongo flask"
for package in $test_packages; do
    if $VENV_PYTHON -c "import $package" 2>/dev/null; then
        echo -e "${GREEN}✓ $package is available${NC}"
    else
        echo -e "${YELLOW}⚠ $package may not be installed correctly${NC}"
    fi
done

# Verify JavaScript test environment
echo -e "${CYAN}Verifying JavaScript test environment...${NC}"
if npm list jest >/dev/null 2>&1; then
    echo -e "${GREEN}✓ Jest is available${NC}"
else
    echo -e "${YELLOW}⚠ Jest may not be installed correctly${NC}"
fi

echo ""
echo -e "${GREEN}=== Setup Complete! ===${NC}"
echo ""
echo -e "${CYAN}To run tests:${NC}"
echo -e "  Python tests: ${GREEN}source .venv/bin/activate && python -m pytest${NC}"
echo -e "  JavaScript tests: ${GREEN}npm test${NC}"
echo ""
echo -e "${CYAN}To activate Python environment:${NC}"
if [[ -f ".venv/bin/activate" ]]; then
    echo -e "  ${GREEN}source .venv/bin/activate${NC}"
else
    echo -e "  ${GREEN}.venv/Scripts/Activate.ps1${NC} (Windows)"
fi
