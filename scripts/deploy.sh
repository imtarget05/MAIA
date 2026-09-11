#!/usr/bin/env bash
# deploy.sh — MAIA deployment verification wrapper
#
# Usage:
#   ./scripts/deploy.sh                        # uses API_BASE_URL from .env
#   API_BASE_URL=https://... ./scripts/deploy.sh   # override on command line
#
# Checks:
#   1. .env file exists
#   2. Required env vars are set (non-empty)
#   3. deploy_check.py runs successfully against API_BASE_URL

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PROJECT_ROOT}/.env"
CHECK_PY="${SCRIPT_DIR}/deploy_check.py"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

pass() { echo -e "  [${GREEN}PASS${NC}] $*"; }
fail() { echo -e "  [${RED}FAIL${NC}] $*"; }
warn() { echo -e "  [${YELLOW}WARN${NC}] $*"; }

echo ""
echo "============================================================"
echo "  MAIA Deployment Verification"
echo "============================================================"

# ------------------------------------------------------------------
# 1. Check .env exists
# ------------------------------------------------------------------
echo ""
echo "[1/3] Checking .env file ..."
if [[ ! -f "${ENV_FILE}" ]]; then
    fail ".env not found at ${ENV_FILE}"
    echo "  Create it from .env.example:  cp .env.example .env"
    exit 1
fi
pass ".env found at ${ENV_FILE}"

# ------------------------------------------------------------------
# 2. Check required env vars
# ------------------------------------------------------------------
echo ""
echo "[2/3] Checking required environment variables ..."

# Load .env (skip comments and blank lines)
set -a
source "${ENV_FILE}"
set +a

REQUIRED_VARS=(
    QDRANT_URL
    QDRANT_COLLECTION
    JWT_SECRET_KEY
    DATA_DIR
)

missing=()
for var in "${REQUIRED_VARS[@]}"; do
    val="${!var:-}"
    if [[ -z "${val}" ]]; then
        fail "${var} is not set (empty)"
        missing+=("${var}")
    else
        pass "${var}=${val}"
    fi
done

# Cloudflare creds are optional — mock mode is valid
if [[ -n "${CLOUDFLARE_ACCOUNT_ID:-}" && -n "${CLOUDFLARE_API_TOKEN:-}" ]]; then
    pass "Cloudflare creds set (real LLM mode)"
else
    warn "Cloudflare creds not set — LLM will run in MOCK mode"
fi

if [[ ${#missing[@]} -gt 0 ]]; then
    echo ""
    fail "Missing required env vars: ${missing[*]}"
    exit 1
fi

# ------------------------------------------------------------------
# 3. Determine API_BASE_URL and run deploy_check.py
# ------------------------------------------------------------------
echo ""
echo "[3/3] Running deploy_check.py ..."

# Priority: CLI arg > env var > default
API_BASE_URL="${API_BASE_URL:-${API_BASE_URL:-}}"
if [[ -z "${API_BASE_URL}" ]]; then
    # Fall back to the API_BASE_URL declared in .env, or localhost
    API_BASE_URL="${API_BASE_URL:-http://localhost:8000}"
    warn "API_BASE_URL not set; defaulting to ${API_BASE_URL}"
else
    pass "API_BASE_URL=${API_BASE_URL}"
fi

echo ""
echo "  Target: ${API_BASE_URL}"
echo ""

# Run the Python checker
set +e
python3 "${CHECK_PY}" "${API_BASE_URL}"
CHECK_EXIT=$?
set -e

echo ""
if [[ ${CHECK_EXIT} -eq 0 ]]; then
    echo -e "  ${GREEN}Deployment verification: PASSED${NC}"
else
    echo -e "  ${RED}Deployment verification: FAILED (exit code ${CHECK_EXIT})${NC}"
fi

echo ""
exit ${CHECK_EXIT}
