#!/usr/bin/env bash
# Setup script for installing/updating JavaScript (Node) dependencies.
#
# DESCRIPTION:
#   Ensures Node.js & npm exist, installs dependencies from package.json.
#   Skips install if node_modules present unless --force used.
#   Outputs jest version after successful install for verification.
#
# FLAGS:
#   --force          Force reinstall (always run npm install)
#   --ci             Reduce output noise for CI (adds --no-audit --no-fund)
#   --no-jest-check  Skip printing jest version after install
#   --help, -h       Show this help message
#
# EXAMPLES:
#   ./setup_js.sh
#   ./setup_js.sh --force
#   ./setup_js.sh --ci
#   ./setup_js.sh --force --ci

set -e

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Parse arguments
FORCE=0
CI=0
NO_JEST_CHECK=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force|-f)
            FORCE=1
            shift
            ;;
        --ci)
            CI=1
            shift
            ;;
        --no-jest-check)
            NO_JEST_CHECK=1
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

# Check for package.json
if [[ ! -f "package.json" ]]; then
    echo -e "${RED}Error: package.json not found in current directory; run from project root.${NC}"
    exit 1
fi

# Check for Node.js
if ! command -v node >/dev/null 2>&1; then
    echo -e "${RED}Error: Node.js is required but was not found. Install from https://nodejs.org/${NC}"
    exit 1
fi

# Check for npm
if ! command -v npm >/dev/null 2>&1; then
    echo -e "${RED}Error: npm is required but was not found (it ships with Node.js).${NC}"
    exit 1
fi

# Display versions
NODE_VERSION=$(node --version 2>/dev/null)
NPM_VERSION=$(npm --version 2>/dev/null)
echo "Node.js ${NODE_VERSION} / npm ${NPM_VERSION}"

# Check if we should skip install
SKIP=0
if [[ $FORCE -eq 0 ]] && [[ -d "node_modules" ]]; then
    echo "node_modules already present; skipping install (use --force to override)."
    SKIP=1
fi

# Install dependencies
if [[ $SKIP -eq 0 ]]; then
    echo "Installing JavaScript dependencies with npm..."
    NPM_ARGS="install"
    if [[ $CI -eq 1 ]]; then
        NPM_ARGS="$NPM_ARGS --no-fund --no-audit"
    fi
    npm $NPM_ARGS
    echo "npm install complete."
else
    echo "Skipped install."
fi

# Check Jest version
if [[ $NO_JEST_CHECK -eq 0 ]]; then
    if JEST_VERSION=$(npm exec jest -- --version 2>/dev/null); then
        echo "jest version: ${JEST_VERSION}"
    else
        echo -e "${YELLOW}jest not found after install (unexpected)${NC}"
        # Don't fail setup just because jest version check failed
    fi
fi

echo -e "${GREEN}=== JavaScript setup done ===${NC}"
