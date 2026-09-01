#!/usr/bin/env bash
# V4.0.0 - Orchestrator version
# Unified environment setup for Von (Python + JavaScript).
#
# DESCRIPTION:
#   Orchestrates Python and JavaScript setup using existing scripts:
#   setup_py.sh and setup_js.sh. Adds flags for selective steps,
#   force reinstall, CI-friendly mode, and Python version selection.
#
# FLAGS:
#   --skip-py           Skip Python environment setup
#   --skip-js           Skip JavaScript dependency setup
#   --force             Force reinstall: passes --reset to setup_py.sh and --force to setup_js.sh
#   --ci                CI mode: quieter JS install, no browser opening side-effects
#   --configure-vscode  Forwarded to setup_py.sh to configure VS Code settings
#   --python-version V  Forwarded to setup_py.sh to target a specific Python minor version
#   --skip-mongodb      Skip MongoDB installation checks (forwarded to setup_py.sh)
#   --with-ocr          Require working image/scanned-PDF OCR; never installs system packages
#   --install-system-deps
#                       Explicitly authorise Tesseract installation; implies --with-ocr
#   --help, -h          Show this help message
#
# EXAMPLES:
#   ./setup_all.sh
#   ./setup_all.sh --configure-vscode
#   ./setup_all.sh --skip-js
#   ./setup_all.sh --skip-py --force
#   ./setup_all.sh --ci --python-version 3.12
#   ./setup_all.sh --skip-mongodb --configure-vscode
#   ./setup_all.sh --install-system-deps

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Parse arguments
SKIP_PY=0
SKIP_JS=0
FORCE=0
CI=0
CONFIGURE_VSCODE=0
PYTHON_VERSION=""
SKIP_MONGODB=0
WITH_OCR=0
INSTALL_SYSTEM_DEPS=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-py)
            SKIP_PY=1
            shift
            ;;
        --skip-js)
            SKIP_JS=1
            shift
            ;;
        --force)
            FORCE=1
            shift
            ;;
        --ci)
            CI=1
            shift
            ;;
        --configure-vscode)
            CONFIGURE_VSCODE=1
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

if [[ $SKIP_PY -eq 1 ]] && { [[ $WITH_OCR -eq 1 ]] || [[ $INSTALL_SYSTEM_DEPS -eq 1 ]]; }; then
    echo -e "${RED}Invalid options: OCR setup cannot be combined with --skip-py.${NC}"
    echo "Run OCR setup as your ordinary user with:"
    echo "  ./setup_all.sh --install-system-deps"
    exit 2
fi

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -d "${HOME}/.local/bin" ]]; then
    export PATH="${HOME}/.local/bin:${PATH}"
fi
OVERALL_OK=1
START_TIME=$(date +%s)

function duration() {
    local start=$1
    local end
    end=$(date +%s)
    echo $((end - start))
}

echo -e "${CYAN}=== Von Unified Setup ===${NC}"
echo "Root: $SCRIPT_DIR"

# Python Phase
if [[ $SKIP_PY -eq 0 ]]; then
    PY_ARGS=""
    if [[ $CONFIGURE_VSCODE -eq 1 ]]; then
        PY_ARGS="$PY_ARGS --configure-vscode"
    fi
    if [[ $FORCE -eq 1 ]]; then
        PY_ARGS="$PY_ARGS --reset"
    fi
    if [[ -n "$PYTHON_VERSION" ]]; then
        PY_ARGS="$PY_ARGS --python-version $PYTHON_VERSION"
    fi
    if [[ $SKIP_MONGODB -eq 1 ]]; then
        PY_ARGS="$PY_ARGS --skip-mongodb"
    fi
    if [[ $WITH_OCR -eq 1 ]]; then
        PY_ARGS="$PY_ARGS --with-ocr"
    fi
    if [[ $INSTALL_SYSTEM_DEPS -eq 1 ]]; then
        PY_ARGS="$PY_ARGS --install-system-deps"
    fi
    
    echo -e "${CYAN}[1/2] Python setup starting (args:$PY_ARGS)${NC}"
    PY_START=$(date +%s)
    
    if "$SCRIPT_DIR/setup_py.sh" $PY_ARGS; then
        PY_DURATION=$(duration $PY_START)
        echo -e "${GREEN}Python setup completed in ${PY_DURATION}s${NC}"
    else
        echo -e "${RED}Python setup failed${NC}"
        OVERALL_OK=0
    fi
else
    echo -e "${YELLOW}[1/2] Python setup skipped (--skip-py)${NC}"
fi

function final_ocr_check() {
    local python_cmd=""
    if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
        python_cmd="$SCRIPT_DIR/.venv/bin/python"
    elif [[ -x "$SCRIPT_DIR/.venv/Scripts/python.exe" ]]; then
        python_cmd="$SCRIPT_DIR/.venv/Scripts/python.exe"
    fi

    [[ -n "$python_cmd" ]] && \
        "$python_cmd" "$SCRIPT_DIR/scripts/verify_tesseract_ocr.py" --quiet
}

function print_final_ocr_unavailable() {
    local required="$1"
    echo "OCR STATUS: UNAVAILABLE"
    echo "Image and scanned-PDF OCR will not work."
    echo "NEXT ACTION: ./setup_all.sh --install-system-deps"
    echo "Run that command from the Von repository as your ordinary user; do not prefix the whole command with sudo."
    echo "VON_SETUP_CAPABILITY name=ocr state=unavailable required=${required}"
    echo 'VON_SETUP_NEXT_ACTION command="./setup_all.sh --install-system-deps"'
}

# JavaScript Phase
if [[ $SKIP_JS -eq 0 ]]; then
    JS_ARGS=""
    if [[ $FORCE -eq 1 ]]; then
        JS_ARGS="$JS_ARGS --force"
    fi
    if [[ $CI -eq 1 ]]; then
        JS_ARGS="$JS_ARGS --ci"
    fi
    
    echo -e "${CYAN}[2/2] JavaScript setup starting (args:$JS_ARGS)${NC}"
    JS_START=$(date +%s)
    
    if "$SCRIPT_DIR/setup_js.sh" $JS_ARGS; then
        JS_DURATION=$(duration $JS_START)
        echo -e "${GREEN}JavaScript setup completed in ${JS_DURATION}s${NC}"
    else
        echo -e "${RED}JavaScript setup failed${NC}"
        OVERALL_OK=0
    fi
else
    echo -e "${YELLOW}[2/2] JavaScript setup skipped (--skip-js)${NC}"
fi

ELAPSED=$(duration $START_TIME)
OCR_OK=0
if final_ocr_check; then
    OCR_OK=1
fi

if [[ $WITH_OCR -eq 1 ]] && [[ $OCR_OK -eq 0 ]]; then
    OVERALL_OK=0
fi

if [[ $OVERALL_OK -eq 1 ]] && [[ $OCR_OK -eq 1 ]]; then
    echo -e "${GREEN}=== FULL SETUP: COMPLETE in ${ELAPSED}s ===${NC}"
    echo "OCR STATUS: AVAILABLE (executable, English data, and real OCR smoke passed)"
    echo "VON_SETUP_CAPABILITY name=ocr state=available required=$([[ $WITH_OCR -eq 1 ]] && echo true || echo false)"
    exit 0
elif [[ $OVERALL_OK -eq 1 ]]; then
    echo -e "${GREEN}=== CORE SETUP: COMPLETE in ${ELAPSED}s ===${NC}"
    print_final_ocr_unavailable false
    exit 0
else
    echo -e "${RED}=== SETUP: INCOMPLETE in ${ELAPSED}s (see above) ===${NC}"
    if [[ $OCR_OK -eq 0 ]]; then
        print_final_ocr_unavailable "$([[ $WITH_OCR -eq 1 ]] && echo true || echo false)"
    fi
    exit 1
fi
