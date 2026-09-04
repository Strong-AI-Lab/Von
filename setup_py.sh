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
#   --with-ocr           Require working OCR; never installs system packages
#   --install-system-deps
#                        Explicitly authorise Tesseract installation; implies --with-ocr
#   --help, -h           Show this help message
#
# EXAMPLES:
#   ./setup_py.sh
#   ./setup_py.sh --configure-vscode
#   ./setup_py.sh --reset
#   ./setup_py.sh --configure-vscode --python-version 3.12
#   ./setup_py.sh --reset --configure-vscode --python-version 3.11
#   ./setup_py.sh --skip-mongodb --configure-vscode
#   ./setup_all.sh --install-system-deps

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
WITH_OCR=0
INSTALL_SYSTEM_DEPS=0

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
        --with-ocr)
            WITH_OCR=1
            shift
            ;;
        --install-system-deps)
            INSTALL_SYSTEM_DEPS=1
            WITH_OCR=1
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -d "${HOME}/.local/bin" ]]; then
    export PATH="${HOME}/.local/bin:${PATH}"
fi

OCR_BASIC_AVAILABLE=0
OCR_REASON="not_checked"
TESS_VERSION=""

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

print_ocr_unavailable() {
    local reason="$1"
    local required="$2"
    echo ""
    echo -e "${YELLOW}================================================================${NC}"
    echo -e "${YELLOW}OCR STATUS: UNAVAILABLE${NC}"
    echo "Reason: $reason"
    echo "Image and scanned-PDF OCR will not work."
    if [[ "$required" == "false" ]]; then
        echo "Core Von setup will continue without OCR."
    else
        echo "OCR was requested, so setup cannot report completion."
    fi
    echo ""
    echo "NEXT ACTION: ./setup_all.sh --install-system-deps"
    echo "Run that command from the Von repository as your ordinary user."
    echo "Do not prefix the whole setup command with sudo."
    echo "VON_SETUP_CAPABILITY name=ocr state=unavailable required=${required}"
    echo 'VON_SETUP_NEXT_ACTION command="./setup_all.sh --install-system-deps"'
    echo -e "${YELLOW}================================================================${NC}"
    echo ""
}

check_tesseract_basic() {
    OCR_REASON=""
    TESS_VERSION=""

    if ! command_exists tesseract; then
        OCR_REASON="the tesseract executable is not on PATH"
        return 1
    fi

    if ! TESS_VERSION=$(tesseract --version 2>/dev/null | head -n1) || [[ -z "$TESS_VERSION" ]]; then
        OCR_REASON="tesseract --version failed"
        return 1
    fi

    local languages
    if ! languages=$(tesseract --list-langs 2>/dev/null); then
        OCR_REASON="tesseract --list-langs failed"
        return 1
    fi
    if ! printf '%s\n' "$languages" | grep -qx 'eng'; then
        OCR_REASON="the required English language data (eng) is missing"
        return 1
    fi

    return 0
}

install_tesseract_debian_user_local() {
    local command_name
    for command_name in apt-get apt-cache dpkg dpkg-query dpkg-deb dpkg-architecture; do
        if ! command_exists "$command_name"; then
            echo -e "${RED}Cannot perform an ordinary-user install: $command_name is unavailable.${NC}"
            return 1
        fi
    done

    local candidate_version architecture safe_version base_dir target_dir current_link bin_dir
    candidate_version=$(apt-cache policy tesseract-ocr | awk '/Candidate:/ {print $2; exit}')
    if [[ -z "$candidate_version" ]] || [[ "$candidate_version" == "(none)" ]]; then
        echo -e "${RED}No installable tesseract-ocr package is present in the current apt package lists.${NC}"
        return 1
    fi
    architecture=$(dpkg --print-architecture 2>/dev/null || dpkg-architecture -qDEB_HOST_ARCH)
    safe_version=$(printf '%s' "$candidate_version" | tr '/:+~' '____')
    base_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/von/system-deps"
    target_dir="${base_dir}/tesseract-${safe_version}-${architecture}"
    current_link="${base_dir}/tesseract-current"
    bin_dir="${HOME}/.local/bin"

    echo -e "${CYAN}sudo is unavailable without a password; installing Ubuntu/Debian Tesseract packages for this ordinary user.${NC}"
    echo "User-local prefix: $target_dir"

    local download_dir staging_dir
    download_dir=$(mktemp -d "${TMPDIR:-/tmp}/von-tesseract.XXXXXX") || return 1
    staging_dir="${download_dir}/root"
    mkdir -p "$staging_dir"

    local -a packages=( )
    local -a queue=(tesseract-ocr tesseract-ocr-eng)
    local -a processed=( )
    local package status existing already_seen dependency package_candidate
    while [[ ${#queue[@]} -gt 0 ]]; do
        package="${queue[0]}"
        queue=("${queue[@]:1}")
        [[ "$package" =~ ^[a-z0-9][a-z0-9+.-]*(:[a-z0-9]+)?$ ]] || continue
        already_seen=0
        for existing in "${processed[@]}"; do
            if [[ "$existing" == "$package" ]]; then
                already_seen=1
                break
            fi
        done
        [[ $already_seen -eq 1 ]] && continue
        processed+=("$package")

        # An installed dependency is already resolved by the host linker and
        # package database; do not recursively download its unrelated subtree.
        status=$(dpkg-query -W -f='${db:Status-Abbrev}' "$package" 2>/dev/null || true)
        if [[ "$status" == ii* ]]; then
            continue
        fi

        package_candidate=$(apt-cache policy "$package" | awk '/Candidate:/ {print $2; exit}')
        if [[ -z "$package_candidate" ]] || [[ "$package_candidate" == "(none)" ]]; then
            echo -e "${RED}No downloadable apt candidate was found for required package $package.${NC}"
            return 1
        fi
        packages+=("$package")

        while IFS= read -r dependency; do
            [[ -n "$dependency" ]] && queue+=("$dependency")
        done < <(
            apt-cache depends --important "$package" 2>/dev/null \
                | sed -nE 's/^[[:space:]]*(Pre)?Depends:[[:space:]]+([^<[:space:]]+).*$/\2/p'
        )
    done

    if [[ ${#packages[@]} -eq 0 ]]; then
        packages=(tesseract-ocr tesseract-ocr-eng)
    fi

    echo "Downloading distro packages without administrative privileges: ${packages[*]}"
    if ! (cd "$download_dir" && apt-get download "${packages[@]}"); then
        rm -rf -- "$download_dir"
        echo -e "${RED}The ordinary-user apt package download failed.${NC}"
        return 1
    fi

    local deb found_deb=0
    for deb in "$download_dir"/*.deb; do
        [[ -f "$deb" ]] || continue
        found_deb=1
        if ! dpkg-deb -x "$deb" "$staging_dir"; then
            rm -rf -- "$download_dir"
            echo -e "${RED}Failed to extract $(basename "$deb").${NC}"
            return 1
        fi
    done
    if [[ $found_deb -eq 0 ]] || [[ ! -x "$staging_dir/usr/bin/tesseract" ]]; then
        rm -rf -- "$download_dir"
        echo -e "${RED}Downloaded packages did not provide a Tesseract executable.${NC}"
        return 1
    fi

    mkdir -p "$base_dir" "$bin_dir"
    if [[ -e "$target_dir" ]]; then
        if [[ ! -x "$target_dir/usr/bin/tesseract" ]]; then
            rm -rf -- "$target_dir"
            mv "$staging_dir" "$target_dir"
        fi
    else
        mv "$staging_dir" "$target_dir"
    fi
    rm -rf -- "$download_dir"

    if [[ -e "$current_link" ]] && [[ ! -L "$current_link" ]]; then
        echo -e "${RED}Cannot update $current_link because it is not a symbolic link.${NC}"
        return 1
    fi
    ln -sfn "$target_dir" "$current_link"

    local wrapper_path="${bin_dir}/tesseract"
    if [[ -e "$wrapper_path" ]] || [[ -L "$wrapper_path" ]]; then
        if ! grep -q '^# Managed by Von setup_all.sh$' "$wrapper_path" 2>/dev/null; then
            echo -e "${RED}Refusing to replace existing non-Von file: $wrapper_path${NC}"
            return 1
        fi
    fi

    local wrapper_tmp="${bin_dir}/.tesseract.von.$$"
    cat > "$wrapper_tmp" <<'WRAPPER'
#!/usr/bin/env sh
# Managed by Von setup_all.sh
set -eu
prefix="${XDG_DATA_HOME:-${HOME}/.local/share}/von/system-deps/tesseract-current"
architecture="$(dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null || true)"
library_path="${prefix}/usr/lib"
if [ -n "$architecture" ]; then
    library_path="${prefix}/usr/lib/${architecture}:${library_path}"
fi
export LD_LIBRARY_PATH="${library_path}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
for candidate in "${prefix}"/usr/share/tesseract-ocr/*/tessdata; do
    if [ -d "$candidate" ]; then
        export TESSDATA_PREFIX="${candidate}/"
        break
    fi
done
exec "${prefix}/usr/bin/tesseract" "$@"
WRAPPER
    chmod 0755 "$wrapper_tmp"
    mv "$wrapper_tmp" "$wrapper_path"
    export PATH="${bin_dir}:${PATH}"
    return 0
}

install_tesseract_system_dependency() {
    case "$(uname -s 2>/dev/null || true)" in
        Darwin)
            if ! command_exists brew; then
                echo -e "${RED}Homebrew is required to install Tesseract on macOS.${NC}"
                return 1
            fi
            echo "Installing Tesseract with: brew install tesseract"
            brew install tesseract
            ;;
        Linux)
            if ! command_exists apt-get; then
                echo -e "${RED}Automatic Tesseract installation currently supports apt-based Linux distributions.${NC}"
                return 1
            fi
            if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
                echo "Installing Tesseract with apt-get (already running as root)."
                apt-get update
                apt-get install -y tesseract-ocr tesseract-ocr-eng
            elif command_exists sudo && sudo -n true >/dev/null 2>&1; then
                echo "Installing Tesseract with: sudo apt-get update"
                sudo -n apt-get update
                echo "Installing Tesseract with: sudo apt-get install -y tesseract-ocr tesseract-ocr-eng"
                sudo -n apt-get install -y tesseract-ocr tesseract-ocr-eng
            else
                install_tesseract_debian_user_local
            fi
            ;;
        *)
            echo -e "${RED}Automatic Tesseract installation is unsupported on this operating system.${NC}"
            return 1
            ;;
    esac
}

prepare_tesseract() {
    echo -e "${CYAN}Checking the Tesseract executable and English language data...${NC}"
    if check_tesseract_basic; then
        OCR_BASIC_AVAILABLE=1
        echo -e "${GREEN}Tesseract basic checks passed: $TESS_VERSION; eng available.${NC}"
        return 0
    fi

    print_ocr_unavailable "$OCR_REASON" "$([[ $WITH_OCR -eq 1 ]] && echo true || echo false)"
    if [[ $INSTALL_SYSTEM_DEPS -eq 0 ]]; then
        [[ $WITH_OCR -eq 0 ]]
        return
    fi

    echo -e "${CYAN}--install-system-deps was provided; installing Tesseract now.${NC}"
    if ! install_tesseract_system_dependency; then
        OCR_REASON="the authorised Tesseract installation command failed"
        print_ocr_unavailable "$OCR_REASON" true
        return 1
    fi
    hash -r
    if ! check_tesseract_basic; then
        print_ocr_unavailable "installation completed but verification failed: $OCR_REASON" true
        return 1
    fi
    OCR_BASIC_AVAILABLE=1
    echo -e "${GREEN}Tesseract installation verified at the executable/language level: $TESS_VERSION; eng available.${NC}"
}

verify_tesseract_runtime() {
    local required="$([[ $WITH_OCR -eq 1 ]] && echo true || echo false)"
    if [[ $OCR_BASIC_AVAILABLE -eq 0 ]]; then
        print_ocr_unavailable "$OCR_REASON" "$required"
        [[ $WITH_OCR -eq 0 ]]
        return
    fi

    if "$VENV_PYTHON" "$SCRIPT_DIR/scripts/verify_tesseract_ocr.py"; then
        echo "OCR STATUS: AVAILABLE"
        echo "VON_SETUP_CAPABILITY name=ocr state=available required=${required}"
        return 0
    fi

    OCR_REASON="the real Pillow/pytesseract OCR smoke test failed"
    print_ocr_unavailable "$OCR_REASON" "$required"
    [[ $WITH_OCR -eq 0 ]]
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
MONGO_ALLOW_LOCAL_FALLBACK=0
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
        if [[ $CONFIGURE_VSCODE -eq 0 ]] && [[ -z "$PYTHON_VERSION" ]] && [[ $WITH_OCR -eq 0 ]]; then
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

    if ! prepare_tesseract; then
        exit 1
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

    # PDM owns project dependency resolution. Keep the bootstrap version new
    # enough to read the committed canonical-input lock format.
    echo -e "${CYAN}Ensuring a compatible PDM is installed...${NC}"
    $VENV_PYTHON -m pip install --upgrade 'pdm==2.29.0'

    # Initialize PDM project if pyproject.toml doesn't exist
    if [[ ! -f "pyproject.toml" ]]; then
        echo -e "${CYAN}Initializing PDM project...${NC}"
        $VENV_PDM init --non-interactive
        echo -e "${GREEN}✓ PDM project initialised${NC}"
    else
        echo -e "${YELLOW}pyproject.toml already exists. Skipping PDM project initialisation.${NC}"
    fi

    if [[ ! -f "pdm.lock" ]]; then
        echo -e "${RED}Error: committed pdm.lock is missing.${NC}"
        exit 1
    fi
    if ! $VENV_PDM lock --check; then
        echo -e "${RED}Error: pdm.lock does not match pyproject.toml.${NC}"
        exit 1
    fi

    # Install exclusively from the reviewed lockfile. Unlike `pdm install`,
    # `pdm sync` never rewrites it. Do not use --clean here because PDM itself
    # is deliberately bootstrapped inside this environment.
    echo -e "${CYAN}Synchronising dependencies from pdm.lock...${NC}"
    if $VENV_PDM sync --verbose; then
        echo -e "${GREEN}✓ PDM successfully synchronised packages${NC}"
    else
        echo -e "${RED}PDM sync failed; the environment was not accepted as ready.${NC}"
        exit 1
    fi

    if ! verify_tesseract_runtime; then
        exit 1
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
