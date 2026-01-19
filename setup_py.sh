#!/usr/bin/env bash
# Setup script for configuring Python environment with PDM and VS Code settings.
#
# DESCRIPTION:
#   This script creates a Python virtual environment, installs PDM within it,
#   installs required packages, and optionally configures VS Code settings.
#   It allows specifying a target Python minor version.
#
# FLAGS:
#   --configure-vscode   Configure VS Code settings
#   --reset              Remove .venv and .vscode directories and start fresh
#   --python-version VER Specify target Python minor version (e.g., "3.12")
#   --skip-mongodb       Skip MongoDB installation checks
#   --help, -h           Show this help message
#
# EXAMPLES:
#   ./setup_py.sh
#   ./setup_py.sh --configure-vscode
#   ./setup_py.sh --reset
#   ./setup_py.sh --configure-vscode --python-version 3.12
#   ./setup_py.sh --reset --configure-vscode --python-version 3.11
#   ./setup_py.sh --skip-mongodb --configure-vscode

set -e

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Parse arguments
CONFIGURE_VSCODE=0
RESET=0
PYTHON_VERSION=""
SKIP_MONGODB=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --configure-vscode)
            CONFIGURE_VSCODE=1
            shift
            ;;
        --reset)
            RESET=1
            shift
            ;;
        --python-version)
            PYTHON_VERSION="$2"
            shift 2
            ;;
        --skip-mongodb)
            SKIP_MONGODB=1
            shift
            ;;
        --help|-h)
            grep "^# " "$0" | sed 's/^# //'
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to reset environment
reset_environment() {
    echo -e "${CYAN}=== Resetting Environment ===${NC}"

    # Remove virtual environment
    if [[ -d ".venv" ]]; then
        echo "Removing .venv directory..."
        if rm -rf ".venv"; then
            echo -e "${GREEN}Removed .venv directory.${NC}"
        else
            echo -e "${RED}Error: Failed to remove the '.venv' directory.${NC}"
            echo -e "${YELLOW}This may be because a process is using it.${NC}"
            echo -e "${YELLOW}Please close all terminals, editors, or processes that might be using the virtual environment and try again.${NC}"
            exit 1
        fi
    else
        echo -e "${YELLOW}.venv directory does not exist. Skipping removal.${NC}"
    fi

    # Remove .vscode directory
    if [[ -d ".vscode" ]]; then
        rm -rf ".vscode"
        echo -e "${GREEN}Removed .vscode directory.${NC}"
    else
        echo -e "${YELLOW}.vscode directory does not exist. Skipping removal.${NC}"
    fi
}

# Function to configure VS Code settings
configure_vscode() {
    echo -e "${CYAN}Setting up VS Code settings...${NC}"

    # Create .vscode directory if it doesn't exist
    mkdir -p .vscode

    # Determine Python path based on OS
    if [[ -f ".venv/bin/python" ]]; then
        PYTHON_PATH=".venv/bin/python"
    else
        PYTHON_PATH=".venv/Scripts/python.exe"
    fi

    # Create or update settings.json
    SETTINGS_FILE=".vscode/settings.json"
    if [[ ! -f "$SETTINGS_FILE" ]]; then
        # Create fresh settings file
        cat > "$SETTINGS_FILE" << EOF
{
  "python.defaultInterpreterPath": "${PYTHON_PATH}",
  "terminal.integrated.defaultProfile.linux": "bash",
  "terminal.integrated.defaultProfile.osx": "bash"
}
EOF
        echo -e "${GREEN}Created $SETTINGS_FILE${NC}"
    else
        echo -e "${YELLOW}$SETTINGS_FILE already exists. Updating...${NC}"
        # Update existing settings using python to preserve other settings
        if command_exists python3 || command_exists python; then
            PYTHON_CMD=$(command_exists python3 && echo "python3" || echo "python")
            $PYTHON_CMD << 'PYEOF'
import json
import sys
import os

try:
    try:
        with open('.vscode/settings.json', 'r') as f:
            settings = json.load(f)
    except:
        settings = {}

    # Determine Python path
    if os.path.exists('.venv/bin/python'):
        python_path = '.venv/bin/python'
    else:
        python_path = '.venv/Scripts/python.exe'

    settings['python.defaultInterpreterPath'] = python_path
    if sys.platform == 'darwin':
        settings['terminal.integrated.defaultProfile.osx'] = 'bash'
    else:
        settings['terminal.integrated.defaultProfile.linux'] = 'bash'

    with open('.vscode/settings.json', 'w') as f:
        json.dump(settings, f, indent=2)

    print('VS Code settings updated successfully.')
except Exception as e:
    print(f'Warning: Failed to update settings.json: {e}', file=sys.stderr)
PYEOF
        else
            # Fallback: just recreate the file
            cat > "$SETTINGS_FILE" << EOF
{
  "python.defaultInterpreterPath": "${PYTHON_PATH}",
  "terminal.integrated.defaultProfile.linux": "bash",
  "terminal.integrated.defaultProfile.osx": "bash"
}
EOF
        fi
        echo -e "${GREEN}VS Code settings updated.${NC}"
    fi

    # Update MCP config to use portable workspace paths
    if [[ -f ".vscode/mcp.json" ]]; then
        if command_exists python3 || command_exists python; then
            PYTHON_CMD=$(command_exists python3 && echo "python3" || echo "python")

            if [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "win32" ]]; then
                MCP_PYTHON='${workspaceFolder}/.venv/Scripts/python.exe'
            else
                MCP_PYTHON='${workspaceFolder}/.venv/bin/python'
            fi
            MCP_STORAGE='${workspaceFolder}/data/arxiv_papers'
            MCP_CWD='${workspaceFolder}'

            MCP_PYTHON="$MCP_PYTHON" MCP_STORAGE="$MCP_STORAGE" MCP_CWD="$MCP_CWD" $PYTHON_CMD << 'PYEOF'
import json
import os
import sys

path = '.vscode/mcp.json'
try:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
except Exception as e:
    print(f'Warning: Failed to parse {path}: {e}', file=sys.stderr)
    sys.exit(0)

servers = data.setdefault('servers', {})

def ensure(name: str):
    server = servers.get(name)
    if not isinstance(server, dict):
        server = {}
        servers[name] = server
    return server

python_path = os.environ.get('MCP_PYTHON') or '${workspaceFolder}/.venv/bin/python'
cwd = os.environ.get('MCP_CWD') or '${workspaceFolder}'
storage = os.environ.get('MCP_STORAGE') or '${workspaceFolder}/data/arxiv_papers'

for name, script in (
    ('vontology', 'src/backend/mcp_server/mcp_stdio_server.py'),
    ('vonrag', 'src/backend/mcp_server/rag_mcp_stdio_server.py'),
):
    server = ensure(name)
    server['type'] = 'stdio'
    server['command'] = python_path
    server['args'] = ['-m', 'pdm', 'run', 'python', script]
    server['cwd'] = cwd

arxiv = ensure('arxiv')
arxiv['type'] = 'stdio'
arxiv['command'] = 'uv'
arxiv['args'] = ['tool', 'run', 'arxiv-mcp-server', '--storage-path', storage]
arxiv['cwd'] = cwd

with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=2)

print('Updated .vscode/mcp.json to use portable workspace paths.')
PYEOF
        else
            echo -e "${YELLOW}Warning: Python not found; skipping MCP config update.${NC}"
        fi
    fi
}

# Function to find compatible Python version
find_python() {
    local target_version="$1"
    local python_cmd=""

    echo -e "${CYAN}Detecting Python installation...${NC}" >&2

    # If specific version requested, try that first
    if [[ -n "$target_version" ]]; then
        echo -e "${CYAN}Attempting to find specified Python version: ${target_version}${NC}" >&2

        # Try python3.X command
        local cmd="python${target_version}"
        if command_exists "$cmd"; then
            if version=$($cmd --version 2>&1); then
                if echo "$version" | grep -q "Python ${target_version}"; then
                    echo -e "${GREEN}Found specified version: $version via '$cmd'${NC}" >&2
                    python_cmd="$cmd"
                    echo "$python_cmd"
                    return 0
                fi
            fi
        fi

        echo -e "${RED}Error: Specified Python version ${target_version} not found.${NC}" >&2
        echo -e "${RED}Please ensure Python ${target_version} is installed and accessible.${NC}" >&2
        exit 1
    fi

    # Default search logic if no specific version requested
    echo -e "${CYAN}No specific version requested, searching for compatible Python (3.10-3.15)...${NC}" >&2

    # Try different Python commands in order of preference
    for cmd in python3.15 python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command_exists "$cmd"; then
            if version=$($cmd --version 2>&1); then
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

    # Try pyenv if available
    if [[ -z "$python_cmd" ]] && command_exists pyenv; then
        echo -e "${CYAN}Detected pyenv, trying to find compatible Python version...${NC}" >&2

        available_versions=$(pyenv versions --bare 2>/dev/null | grep -E '^3\.(1[0-5]|[0-9]+)\.' | sort -V -r)

        for version in $available_versions; do
            major_minor=$(echo "$version" | cut -d. -f1,2)
            minor_version=$(echo "$major_minor" | cut -d. -f2)

            if [[ "$minor_version" -ge 10 ]]; then
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

    if [[ -z "$python_cmd" ]]; then
        echo -e "${RED}Error: Compatible Python (3.10-3.15) not found via python, python3, or python3.X.${NC}" >&2
        echo -e "${RED}Please install a compatible Python version from https://www.python.org/downloads/${NC}" >&2
        if command_exists pyenv; then
            echo -e "${YELLOW}Or install via pyenv: pyenv install 3.12.0 && pyenv global 3.12.0${NC}" >&2
        fi
        exit 1
    fi

    echo "$python_cmd"
}

# Function to ensure .env file exists
ensure_env_file() {
    echo ""
    echo -e "${CYAN}=== Database Configuration ===${NC}"

    if [[ ! -f ".env" ]]; then
        echo -e "${YELLOW}No .env file found. Creating from template...${NC}"

        if [[ -f ".env.template" ]]; then
            cp ".env.template" ".env"
            echo -e "${GREEN}✓ Created .env from .env.template${NC}"
            echo -e "  You can edit .env to customise database and API settings"
        elif [[ -f ".env.example" ]]; then
            cp ".env.example" ".env"
            echo -e "${GREEN}✓ Created .env from .env.example${NC}"
            echo -e "  You can edit .env to customise database and API settings"
        else
            echo -e "${YELLOW}✗ No .env template found. Creating minimal .env...${NC}"
            cat > .env << 'EOF'
MONGO_URI=mongodb://localhost:27017/
VON_DB_NAME=von_db
MONGO_LOCAL_URI=mongodb://127.0.0.1:27017/?directConnection=true
MONGO_ALLOW_LOCAL_FALLBACK=1
OLLAMA_HOSTS_LIST=127.0.0.1
EOF
            echo -e "${GREEN}✓ Created minimal .env file${NC}"
        fi
    else
        echo -e "${GREEN}.env file already exists. Skipping creation.${NC}"
    fi
}

# Main setup logic
main() {
    # Handle reset if requested
    if [[ $RESET -eq 1 ]]; then
        reset_environment
        # Check if ONLY reset was requested
        if [[ $CONFIGURE_VSCODE -eq 0 ]] && [[ -z "$PYTHON_VERSION" ]]; then
            echo -e "${GREEN}Environment reset complete. Exiting as no setup flags were provided.${NC}"
            exit 0
        fi
    fi

    # MongoDB check (Linux/macOS version - manual install required)
    if [[ $SKIP_MONGODB -eq 0 ]]; then
        echo -e "${CYAN}Checking for MongoDB...${NC}"
        if command_exists mongod; then
            MONGO_VERSION=$(mongod --version | head -n1)
            echo -e "${GREEN}MongoDB found: $MONGO_VERSION${NC}"
        else
            echo -e "${YELLOW}MongoDB not found. Please install MongoDB manually:${NC}"
            echo -e "  macOS: ${CYAN}brew install mongodb-community${NC}"
            echo -e "  Ubuntu/Debian: ${CYAN}sudo apt install mongodb${NC}"
            echo -e "  Or download from: https://www.mongodb.com/try/download/community"
            echo -e "${YELLOW}Continuing with Python setup...${NC}"
        fi
    else
        echo -e "${YELLOW}Skipping MongoDB installation checks (-skip-mongodb flag set).${NC}"
    fi

    echo -e "${CYAN}Checking for Tesseract OCR...${NC}"
    if command_exists tesseract; then
        TESS_VERSION=$(tesseract --version | head -n1)
        echo -e "${GREEN}Tesseract OCR found: $TESS_VERSION${NC}"
    else
        echo -e "${YELLOW}Tesseract OCR not found. Please install manually:${NC}"
        echo -e "  macOS: ${CYAN}brew install tesseract${NC}"
        echo -e "  Ubuntu/Debian: ${CYAN}sudo apt install tesseract-ocr${NC}"
        echo -e "  Or see: https://github.com/tesseract-ocr/tesseract"
        echo -e "${YELLOW}Continuing with Python setup...${NC}"
    fi

    # Ensure .env file exists
    ensure_env_file

    # Find Python
    PYTHON_CMD=$(find_python "$PYTHON_VERSION")
    echo -e "${GREEN}Using Python command: $PYTHON_CMD${NC}"

    # Setup Python environment
    echo -e "${CYAN}=== Running Python Setup ===${NC}"

    # Determine venv paths based on OS
    if [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "win32" ]]; then
        VENV_PYTHON=".venv/Scripts/python.exe"
        VENV_PDM=".venv/Scripts/pdm.exe"
    else
        VENV_PYTHON=".venv/bin/python"
        VENV_PDM=".venv/bin/pdm"
    fi

    # Create virtual environment if it doesn't exist
    if [[ ! -f "$VENV_PYTHON" ]]; then
        echo -e "${CYAN}Creating virtual environment...${NC}"
        $PYTHON_CMD -m venv .venv

        if [[ ! -f "$VENV_PYTHON" ]]; then
            echo -e "${RED}Error: Failed to create virtual environment.${NC}"
            exit 1
        fi
        echo -e "${GREEN}✓ Virtual environment created${NC}"
    else
        echo -e "${YELLOW}Virtual environment already exists. Skipping creation.${NC}"
    fi

    # Upgrade pip
    echo -e "${CYAN}Updating pip in virtual environment...${NC}"
    $VENV_PYTHON -m pip install --upgrade pip

    # Install PDM if not present
    if ! $VENV_PYTHON -m pip show pdm >/dev/null 2>&1; then
        echo -e "${CYAN}Installing PDM in virtual environment...${NC}"
        $VENV_PYTHON -m pip install pdm
        echo -e "${GREEN}✓ PDM installed${NC}"
    else
        echo -e "${YELLOW}PDM is already installed in the virtual environment. Skipping installation.${NC}"
    fi

    # Initialize PDM project if pyproject.toml doesn't exist
    if [[ ! -f "pyproject.toml" ]]; then
        echo -e "${CYAN}Initializing PDM project...${NC}"
        $VENV_PDM init --non-interactive
        echo -e "${GREEN}✓ PDM project initialised${NC}"
    else
        echo -e "${YELLOW}pyproject.toml already exists. Skipping PDM project initialisation.${NC}"
    fi

    # Install dependencies with PDM
    echo -e "${CYAN}Installing dependencies with PDM...${NC}"
    if $VENV_PDM install --verbose; then
        echo -e "${GREEN}✓ PDM successfully installed packages${NC}"
    else
        echo -e "${YELLOW}⚠ PDM install encountered issues${NC}"
        echo -e "${YELLOW}  Trying alternative installation method...${NC}"

        # Fallback: install critical packages with pip
        CRITICAL_PACKAGES="flask pymongo python-dotenv requests pytest pytest-mock pytest-cov mongomock"
        echo -e "${CYAN}Installing critical packages with pip...${NC}"
        $VENV_PYTHON -m pip install $CRITICAL_PACKAGES

        # Try PDM again with --no-sync
        echo -e "${CYAN}Retrying PDM install...${NC}"
        if $VENV_PDM install --verbose --no-sync; then
            echo -e "${GREEN}✓ Python dependencies installed via fallback${NC}"
        else
            echo -e "${YELLOW}⚠ Some Python packages may not be installed correctly${NC}"
        fi
    fi

    # Configure VS Code if requested
    if [[ $CONFIGURE_VSCODE -eq 1 ]]; then
        configure_vscode
    fi

    echo ""
    echo -e "${GREEN}=== Setup Complete! ===${NC}"
    echo ""
    echo -e "${CYAN}To activate the virtual environment:${NC}"
    if [[ -f ".venv/bin/activate" ]]; then
        echo -e "  ${GREEN}source .venv/bin/activate${NC}"
    else
        echo -e "  ${GREEN}.venv/Scripts/Activate.ps1${NC} (Windows)"
    fi
}

# Run main function
main
